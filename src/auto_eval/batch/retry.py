"""可重试异常与重试调用。"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

from ..llm_stream import is_retriable_llm_error
from ..observability import current_context, error_details, log_event


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
                log_event(
                    current_context().module or "单题评测",
                    "调用失败，不可重试",
                    level=logging.ERROR,
                    details=error_details(e),
                )
                raise
            last_exc = e
            if i == max_attempts - 1:
                break
            cap = min(max_wait, base_wait * (2**i))
            wait = random.uniform(0.0, cap)
            log_event(
                current_context().module or "单题评测",
                "调用失败，准备外层重试",
                level=logging.WARNING,
                details={
                    "请求次数": f"{i + 1}/{max_attempts}",
                    "等待": f"{wait:.2f}秒",
                    **error_details(e, include_traceback=False),
                },
            )
            await asyncio.sleep(wait)
    if last_exc is not None:
        log_event(
            current_context().module or "单题评测",
            "外层重试最终失败",
            level=logging.ERROR,
            details={
                "请求次数": f"{max_attempts}/{max_attempts}",
                **error_details(last_exc),
            },
        )
    raise last_exc  # type: ignore[misc]
