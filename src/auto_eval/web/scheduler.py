"""全局调度：跨任务共享的两级并发限流 + 运行时设置（并发/等待容量/超时/裁判）。

两级闸门，获取顺序恒为 PIPELINE → MODEL，无反向嵌套：
- PIPELINE_LIMITER（上限 = concurrency + waiting_capacity）：流水线准入，
  调度单位是「整个 session 组」「单条独立题」或「一个更新批」——最多这么
  多个评测单元处于准入后状态（预处理中 / 等模型槽 / 模型调用中），界定
  「最多积累 waiting_capacity 个等待的评测」。
- MODEL_LIMITER（上限 = concurrency）：模型调用级，单题在 one() 内进入
  模型调用阶段时获取——视频预处理不占模型槽，模型并发始终跑满。组第 2+
  轮 / 批第 2+ 条 priority=True 插到模型队列队头（组内轮次不被排到队尾）。

并发、等待容量、单题超时与裁判由 GET/PUT /api/settings 全局管理，
持久化到 runs/web_settings.json（runs/ 不入库，重启后加载）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import RUNS_DIR


logger = logging.getLogger(__name__)

SETTINGS_PATH = RUNS_DIR / "web_settings.json"
DEFAULT_CONCURRENCY = 10  # 模型限流 ~10 req/s 建模为模型调用级并发 10
DEFAULT_WAITING_CAPACITY = 10  # 模型槽满时可继续做预处理的等待评测数
DEFAULT_EVAL_TIMEOUT_S = 300.0
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 64
MIN_WAITING_CAPACITY, MAX_WAITING_CAPACITY = 0, 256
MIN_EVAL_TIMEOUT_S, MAX_EVAL_TIMEOUT_S = 30.0, 3600.0


@dataclass
class RuntimeSettings:
    concurrency: int = DEFAULT_CONCURRENCY
    waiting_capacity: int = DEFAULT_WAITING_CAPACITY
    eval_timeout_s: float = DEFAULT_EVAL_TIMEOUT_S
    # 裁判名列表（本模块不感知 config，只存原始名字；空 = 未设置，由
    # 调用方回落到配置的第一个裁判）。合法性校验在 server PUT 侧做。
    judges: list[str] = field(default_factory=list)


class ResizableLimiter:
    """FIFO 全局并发槽；运行中可调上限。

    不用 asyncio.Semaphore/Condition：它们在首次挂起时绑定事件循环，
    pytest-asyncio 每个测试新建循环会报 "bound to a different event loop"。
    本实现的等待 future 每次 acquire 现取 running loop 创建，跨循环安全。

    调大上限立即唤醒队头等待者；调小为软限制——不抢占运行中的任务，
    新的 acquire 按 `running < limit` 门控，运行完自然收敛。

    acquire(priority=True) 把等待者插到队头（_wake 的 popleft 天然先放行），
    供 session 组第 2+ 轮 / 批第 2+ 条优先续队：不排到新到达者之后。多个
    优先者之间为 LIFO——每组同一时刻至多一个优先等待者（组内轮次串行），
    有界可接受。取消清理（按 future 恒等出队 / 已预占则归还）与队列位置
    无关，priority 路径与普通路径同等安全。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._running = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    def stats(self) -> dict:
        return {
            "limit": self._limit,
            "running": self._running,
            "queued": sum(1 for w in self._waiters if not w.done()),
        }

    def set_limit(self, limit: int) -> None:
        new = max(1, int(limit))
        if new != self._limit:
            self._limit = new
            self._wake()  # 调大时立刻放行队头；调小时 _wake 自行按新上限门控

    def _wake(self) -> None:
        while self._waiters and self._running < self._limit:
            waiter = self._waiters.popleft()
            if waiter.done():  # 已被取消的滞留者
                continue
            # 先预占槽位再放行，防止连续唤醒超发
            self._running += 1
            waiter.set_result(None)

    async def acquire(self, *, priority: bool = False) -> None:
        if not self._waiters and self._running < self._limit:
            # 有等待者时禁止插队，保证 FIFO（显式 priority 除外）
            self._running += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(waiter)
        else:
            self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.cancelled() or not waiter.done():
                # 从未取得槽位：仅出队
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            else:
                # _wake 已替本协程预占槽位但协程仍被取消：必须归还，
                # 否则计数泄漏、后续请求被永久少一个槽
                self.release()
            raise

    def release(self) -> None:
        self._running = max(0, self._running - 1)
        self._wake()

    def would_block(self) -> bool:
        """现在 acquire 是否需要排队（供展示层决定是否发「等待槽位」事件）。"""
        return bool(self._waiters) or self._running >= self._limit

    async def __aenter__(self) -> "ResizableLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        """带参数的槽位 async CM（`async with lim:` 语法无法给 __aenter__ 传参）。"""
        return _LimiterSlot(self, priority)


class _LimiterSlot:
    def __init__(self, limiter: ResizableLimiter, priority: bool) -> None:
        self._limiter = limiter
        self._priority = priority

    async def __aenter__(self) -> ResizableLimiter:
        await self._limiter.acquire(priority=self._priority)
        return self._limiter

    async def __aexit__(self, *exc) -> None:
        self._limiter.release()


MODEL_LIMITER = ResizableLimiter(DEFAULT_CONCURRENCY)
PIPELINE_LIMITER = ResizableLimiter(DEFAULT_CONCURRENCY + DEFAULT_WAITING_CAPACITY)

_settings = RuntimeSettings()


def get_settings() -> RuntimeSettings:
    return _settings


def apply_settings(
    *,
    concurrency: int | None = None,
    waiting_capacity: int | None = None,
    eval_timeout_s: float | None = None,
    judges: list[str] | None = None,
) -> RuntimeSettings:
    """更新运行时设置并即时对齐限流器（越界值 clamp 到合法区间）。

    任一参数单独变化都从 _settings 读对方现值重算 PIPELINE 上限
    （= concurrency + waiting_capacity），避免两参非原子更新时出现
    pipeline < model 的病态；waiting_capacity=0 是安全退化（无预取缓冲）。
    """
    global _settings
    if concurrency is not None:
        _settings.concurrency = min(
            MAX_CONCURRENCY, max(MIN_CONCURRENCY, int(concurrency))
        )
        MODEL_LIMITER.set_limit(_settings.concurrency)
        PIPELINE_LIMITER.set_limit(_settings.concurrency + _settings.waiting_capacity)
    if waiting_capacity is not None:
        _settings.waiting_capacity = min(
            MAX_WAITING_CAPACITY, max(MIN_WAITING_CAPACITY, int(waiting_capacity))
        )
        PIPELINE_LIMITER.set_limit(_settings.concurrency + _settings.waiting_capacity)
    if eval_timeout_s is not None:
        _settings.eval_timeout_s = min(
            MAX_EVAL_TIMEOUT_S, max(MIN_EVAL_TIMEOUT_S, float(eval_timeout_s))
        )
    if judges is not None:
        _settings.judges = list(dict.fromkeys(str(name) for name in judges))
    return _settings


def load_persisted_settings(path: Path | None = None) -> RuntimeSettings:
    """启动时加载持久化设置；文件缺失/损坏一律回退默认值，绝不阻断启动。"""
    global _settings
    target = path or SETTINGS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    concurrency = None
    waiting_capacity = None
    eval_timeout_s = None
    judges = None
    if isinstance(raw, dict):
        if isinstance(raw.get("concurrency"), int):
            concurrency = raw["concurrency"]
        if isinstance(raw.get("waiting_capacity"), int):
            waiting_capacity = raw["waiting_capacity"]
        if isinstance(raw.get("eval_timeout_s"), (int, float)):
            eval_timeout_s = float(raw["eval_timeout_s"])
        persisted_judges = raw.get("judges")
        if isinstance(persisted_judges, list) and all(
            isinstance(name, str) for name in persisted_judges
        ):
            judges = persisted_judges
    apply_settings(
        concurrency=concurrency,
        waiting_capacity=waiting_capacity,
        eval_timeout_s=eval_timeout_s,
        judges=judges,
    )
    return _settings


def persist_settings(path: Path | None = None) -> bool:
    """原子写（tmp + os.replace，镜像 history.save_task 的模式）。best-effort。"""
    target = path or SETTINGS_PATH
    payload = {
        "concurrency": _settings.concurrency,
        "waiting_capacity": _settings.waiting_capacity,
        "eval_timeout_s": _settings.eval_timeout_s,
        "judges": _settings.judges,
        "updated_at": time.time(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return True
    except OSError:
        logger.exception("保存系统设置失败: %s", target)
        return False
