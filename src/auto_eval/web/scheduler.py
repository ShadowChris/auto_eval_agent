"""全局调度：跨任务共享的可调并发限流器 + 运行时设置（系统并发/单题超时/裁判）。

所有评测请求（全量跑批、更新批、独立题）竞争同一个 EVAL_LIMITER 槽位；
调度单位是「整个 session 组」或「单条独立题」，组内轮次背靠背执行，
不再每轮重新排队。并发、单题超时与裁判由 GET/PUT /api/settings 全局管理，
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
DEFAULT_CONCURRENCY = 10  # 模型限流 ~10 req/s 建模为全局并发 10
DEFAULT_EVAL_TIMEOUT_S = 300.0
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 64
MIN_EVAL_TIMEOUT_S, MAX_EVAL_TIMEOUT_S = 30.0, 3600.0


@dataclass
class RuntimeSettings:
    concurrency: int = DEFAULT_CONCURRENCY
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

    async def acquire(self) -> None:
        if not self._waiters and self._running < self._limit:
            # 有等待者时禁止插队，保证 FIFO
            self._running += 1
            return
        waiter = asyncio.get_running_loop().create_future()
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

    async def __aenter__(self) -> "ResizableLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()


EVAL_LIMITER = ResizableLimiter(DEFAULT_CONCURRENCY)

_settings = RuntimeSettings()


def get_settings() -> RuntimeSettings:
    return _settings


def apply_settings(
    *,
    concurrency: int | None = None,
    eval_timeout_s: float | None = None,
    judges: list[str] | None = None,
) -> RuntimeSettings:
    """更新运行时设置并即时对齐限流器（越界值 clamp 到合法区间）。"""
    global _settings
    if concurrency is not None:
        _settings.concurrency = min(
            MAX_CONCURRENCY, max(MIN_CONCURRENCY, int(concurrency))
        )
        EVAL_LIMITER.set_limit(_settings.concurrency)
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
    eval_timeout_s = None
    judges = None
    if isinstance(raw, dict):
        if isinstance(raw.get("concurrency"), int):
            concurrency = raw["concurrency"]
        if isinstance(raw.get("eval_timeout_s"), (int, float)):
            eval_timeout_s = float(raw["eval_timeout_s"])
        persisted_judges = raw.get("judges")
        if isinstance(persisted_judges, list) and all(
            isinstance(name, str) for name in persisted_judges
        ):
            judges = persisted_judges
    apply_settings(
        concurrency=concurrency, eval_timeout_s=eval_timeout_s, judges=judges
    )
    return _settings


def persist_settings(path: Path | None = None) -> bool:
    """原子写（tmp + os.replace，镜像 history.save_task 的模式）。best-effort。"""
    target = path or SETTINGS_PATH
    payload = {
        "concurrency": _settings.concurrency,
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
