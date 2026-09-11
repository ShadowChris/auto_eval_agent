"""进程内共享的异步模型请求限速器。"""
from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections.abc import Awaitable, Callable


class SmoothRequestRateLimiter:
    """按固定间隔平滑发放请求槽位，避免窗口边界产生瞬时突发。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self, max_requests: int, window_seconds: float) -> float:
        requests = max(1, int(max_requests))
        window = max(0.001, float(window_seconds))
        interval = window / requests
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + interval
            wait_seconds = max(0.0, slot - now)
        if wait_seconds > 0:
            await self._sleep(wait_seconds)
        return wait_seconds


_REGISTRY_LOCK = threading.Lock()
_LOOP_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, SmoothRequestRateLimiter],
] = weakref.WeakKeyDictionary()


def shared_request_rate_limiter(scope_key: str) -> SmoothRequestRateLimiter:
    """同一事件循环内按 Provider/API 作用域共享请求时间线。"""
    loop = asyncio.get_running_loop()
    normalized_key = str(scope_key or "default").strip() or "default"
    with _REGISTRY_LOCK:
        limiters = _LOOP_LIMITERS.setdefault(loop, {})
        limiter = limiters.get(normalized_key)
        if limiter is None:
            limiter = SmoothRequestRateLimiter()
            limiters[normalized_key] = limiter
        return limiter


async def acquire_request_slot(
    scope_key: str,
    *,
    max_requests: int,
    window_seconds: float,
) -> float:
    limiter = shared_request_rate_limiter(scope_key)
    return await limiter.acquire(max_requests, window_seconds)

