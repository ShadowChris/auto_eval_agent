"""可重试异常与重试调用。"""
from __future__ import annotations

import asyncio
import random

import httpx

from ..llm_stream import is_retriable_llm_error


def _is_retriable(exc: BaseException) -> bool:
    if is_retriable_llm_error(exc):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 409, 429, 500, 502, 503, 504}
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


async def retry_call(coro_factory, *, max_attempts: int = 4, base_wait: float = 1.0, max_wait: float = 30.0):
    """对 `coro_factory()`（返回协程的零参函数）做指数退避重试，仅重试 RETRIABLE 异常。"""
    last_exc = None
    for i in range(max_attempts):
        try:
            return await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not _is_retriable(e):
                raise
            last_exc = e
            if i == max_attempts - 1:
                break
            cap = min(max_wait, base_wait * (2**i))
            await asyncio.sleep(random.uniform(0.0, cap))
    raise last_exc  # type: ignore[misc]
