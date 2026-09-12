"""全局调度：跨任务共享的速率限流 + 预处理并发 + 运行时设置（速率/容量/超时/裁判）。

两级闸门，获取顺序恒为 PIPELINE → MODEL，无反向嵌套：
- PIPELINE_LIMITER（上限 = waiting_capacity）：视频预处理并发闸，只在 one()
  的视频抽帧期间持有，抽出即释放——不限制模型在途请求。已带 frames 的重跑
  题不走预处理、不占槽。
- MODEL_LIMITER（速率 = concurrency 令牌/秒，无在途上限）：模型调用级速率
  限流，令牌桶——每秒至多发起 concurrency 次模型请求（含重试的每一次尝试），
  申请到令牌即发出、不等待完成、不限制在途。组第 2+ 轮 / 批第 2+ 条首次
  尝试 priority=True 插队头（组内轮次不被排到队尾）。

速率（concurrency，每秒请求数）、预处理并发（waiting_capacity）、单题超时
与裁判由 GET/PUT /api/settings 全局管理，持久化到 runs/web_settings.json
（runs/ 不入库，重启后加载）。字段名保留 concurrency/waiting_capacity 仅语义
改变（旧设置值兼容，无需迁移）。
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
DEFAULT_CONCURRENCY = 10  # 模型调用速率限流：每秒至多 10 次请求（不限制在途）
DEFAULT_WAITING_CAPACITY = 10  # 视频预处理并发上限：同时抽帧的评测数
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


class TokenBucketRateLimiter:
    """模型调用级速率限流：令牌桶，每秒至多 `rate` 个令牌（突发 = rate）。

    与 ResizableLimiter（并发槽）不同：acquire 申请到令牌即返回、**不占在途
    容量**——每次实际模型请求（含重试）消耗一个每秒配额，发出后不等待完成、
    在途无上限。release() 不恢复容量（速率是时间制而非占用制），仅维护展示
    用在途计数。

    实现：快路径直接消费存量令牌；无令牌时压入等待队列（priority=True 经
    appendleft 插队头），后台 minter 协程按当前 rate 间隔喂令牌给队头等待者。
    取消清理按 future 恒等出队，跨事件循环安全（等待 future 每次现取 running
    loop 创建）。

    set_rate 调大立即以新间隔放行；调小为软生效（已排队等待者按新 rate 重算
    间隔）。release 幂等（在途计数不越界）。
    """

    def __init__(self, rate: int) -> None:
        self._rate = max(1, int(rate))
        self._tokens = float(self._rate)  # 突发 = rate；时间制，无持有
        self._last: float | None = None  # 上次补币的 loop 时间戳（None=未初始化）
        self._in_flight = 0  # 仅展示用在途计数，不是限制
        self._waiters: deque[asyncio.Future] = deque()
        self._minter: asyncio.Task | None = None

    @property
    def limit(self) -> int:
        return self._rate

    def stats(self) -> dict:
        return {
            "limit": self._rate,
            "running": self._in_flight,
            "queued": sum(1 for w in self._waiters if not w.done()),
        }

    def set_rate(self, rate: int) -> None:
        new = max(1, int(rate))
        if new != self._rate:
            self._rate = new
            self._ensure_minter()

    def would_block(self) -> bool:
        """现在 acquire 是否要等待令牌（供展示层决定是否发「等待」事件）。"""
        return self._tokens < 1.0 or bool(self._waiters)

    def _refill(self, now: float) -> None:
        if self._last is None:
            self._last = now  # 首次基线：只记录时间戳，不凭空补突发
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last = now

    def _ensure_minter(self) -> None:
        if self._waiters and (
            self._minter is None or self._minter.done() or self._minter.cancelled()
        ):
            self._minter = asyncio.create_task(self._refill_loop())

    async def acquire(self, *, priority: bool = False) -> None:
        if not self._waiters and self._tokens >= 1.0:
            # 快路径：存量令牌足够且无人排队，直接消费
            self._tokens -= 1.0
            self._in_flight += 1
            return
        fut = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(fut)
        else:
            self._waiters.append(fut)
        self._ensure_minter()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.cancelled() or not fut.done():
                # 从未取得令牌：仅出队（minter 也未喂，未计在途）
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            else:
                # 竞态兜底：minter 已喂令牌但协程被取消——归还展示计数
                self._in_flight = max(0, self._in_flight - 1)
            raise

    async def _refill_loop(self) -> None:
        """把令牌按 rate 间隔喂给队头等待者；队列清空即退出。"""
        while self._waiters:
            now = asyncio.get_running_loop().time()
            self._refill(now)
            if self._tokens < 1.0:
                await asyncio.sleep((1.0 - self._tokens) / self._rate)
                continue  # 循环顶部重补币；rate/priority 可随后续请求变化
            waiter = self._waiters.popleft()
            if waiter.done():  # 已被取消清理的滞留者
                continue
            self._tokens -= 1.0
            self._in_flight += 1
            waiter.set_result(None)

    def release(self) -> None:
        # 速率制：token 已按时间恢复，release 不补回；只降展示计数
        self._in_flight = max(0, self._in_flight - 1)

    async def __aenter__(self) -> "TokenBucketRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        return _LimiterSlot(self, priority)


MODEL_LIMITER: ResizableLimiter | TokenBucketRateLimiter = TokenBucketRateLimiter(
    DEFAULT_CONCURRENCY
)
PIPELINE_LIMITER = ResizableLimiter(DEFAULT_WAITING_CAPACITY)

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

    concurrency = 模型调用速率（令牌/秒，无在途上限）→ MODEL_LIMITER.set_rate；
    waiting_capacity = 视频预处理并发 → PIPELINE_LIMITER.set_limit。两参独立、
    不再联动——模型在途不再受流水线闸门约束。waiting_capacity=0 时
    ResizableLimiter 自动 clamp 到 >=1（最严也保留单路预处理）。
    """
    global _settings
    if concurrency is not None:
        _settings.concurrency = min(
            MAX_CONCURRENCY, max(MIN_CONCURRENCY, int(concurrency))
        )
        MODEL_LIMITER.set_rate(_settings.concurrency)
    if waiting_capacity is not None:
        _settings.waiting_capacity = min(
            MAX_WAITING_CAPACITY, max(MIN_WAITING_CAPACITY, int(waiting_capacity))
        )
        PIPELINE_LIMITER.set_limit(max(1, _settings.waiting_capacity))
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
