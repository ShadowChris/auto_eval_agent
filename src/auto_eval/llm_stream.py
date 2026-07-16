"""OpenAI 兼容接口的流式调用、完整响应聚合与重试。"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from .observability import current_context, error_details, log_event


class StreamProtocolError(RuntimeError):
    """流正常关闭，但没有返回可用的 choice。"""


class ProviderStreamError(RuntimeError):
    """HTTP 连接正常，但流式 chunk 通过 error 字段报告服务端错误。"""

    def __init__(
        self,
        message: str,
        *,
        body: Any,
        status_code: int | None = None,
        retriable: bool = False,
    ):
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.retriable = retriable


@dataclass
class AggregatedFunction:
    name: str
    arguments: str


@dataclass
class AggregatedToolCall:
    id: str
    type: str
    function: AggregatedFunction


@dataclass
class AggregatedMessage:
    content: str
    role: str = "assistant"
    tool_calls: list[AggregatedToolCall] | None = None


@dataclass
class AggregatedChoice:
    index: int
    message: AggregatedMessage
    finish_reason: str | None = None


@dataclass
class AggregatedResponse:
    choices: list[AggregatedChoice]
    model: str = ""
    usage: Any = None


def build_openai_client(
    *,
    base_url: str,
    api_key: str,
    connect_timeout_s: float,
    read_timeout_s: float,
):
    """创建禁用 SDK 内置重试的客户端，由本模块统一控制重试次数。"""
    from openai import AsyncOpenAI

    timeout = httpx.Timeout(
        timeout=read_timeout_s,
        connect=connect_timeout_s,
        read=read_timeout_s,
        write=read_timeout_s,
        pool=connect_timeout_s,
    )
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )


def _status_code(exc: BaseException) -> int | None:
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


def is_retriable_llm_error(exc: BaseException) -> bool:
    """只重试瞬时错误，避免对鉴权和参数错误反复请求。"""
    if isinstance(exc, ProviderStreamError):
        return exc.retriable
    if isinstance(
        exc,
        (
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            StreamProtocolError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    if isinstance(exc, APIStatusError):
        return _status_code(exc) in {408, 409, 429, 500, 502, 503, 504}
    return False


def _error_value(error: Any, key: str) -> Any:
    if isinstance(error, dict):
        return error.get(key)
    return getattr(error, key, None)


def _provider_stream_error(error: Any) -> ProviderStreamError:
    """兼容 error 为字符串、dict 或 SDK 动态对象的网关响应。"""
    message = _error_value(error, "message") or _error_value(error, "detail")
    code = _error_value(error, "code")
    error_type = _error_value(error, "type")
    status = (
        _error_value(error, "status_code")
        or _error_value(error, "status")
        or _error_value(error, "http_status")
    )
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None

    if not message:
        message = str(error)
    signal = " ".join(str(v) for v in (code, error_type, message) if v).lower()
    retriable = (
        status_code in {408, 409, 429, 500, 502, 503, 504}
        or any(
            marker in signal
            for marker in (
                "aborted",
                "timeout",
                "rate_limit",
                "rate limit",
                "too many requests",
                "overloaded",
                "server_error",
                "service unavailable",
                "temporarily unavailable",
                "系统繁忙",
                "请求过于频繁",
                "限流",
            )
        )
    )
    return ProviderStreamError(
        f"服务端流式错误：{message}",
        body=error,
        status_code=status_code,
        retriable=retriable,
    )


def _rejects_stream_usage(exc: BaseException) -> bool:
    if not isinstance(exc, APIStatusError) or _status_code(exc) not in {400, 422}:
        return False
    detail = f"{exc} {getattr(exc, 'body', '')}".lower()
    return "stream_options" in detail or "include_usage" in detail


async def _collect_stream(client, kwargs: dict, *, include_usage: bool):
    request = {**kwargs, "stream": True}
    if include_usage:
        request["stream_options"] = {"include_usage": True}

    stream = await client.chat.completions.create(**request)
    content_chunks: list[str] = []
    tool_chunks: dict[int, dict[str, str]] = {}
    finish_reason = None
    usage = None
    model = kwargs.get("model", "")
    saw_choice = False
    started = time.perf_counter()
    stats = {
        "chunk数": 0,
        "输出字符": 0,
        "工具参数字符": 0,
        "首Token耗时": None,
    }

    try:
        async for chunk in stream:
            stats["chunk数"] += 1
            usage = getattr(chunk, "usage", None) or usage
            model = getattr(chunk, "model", None) or model
            chunk_error = getattr(chunk, "error", None)
            if chunk_error is None:
                chunk_error = (getattr(chunk, "model_extra", None) or {}).get("error")
            if chunk_error is not None:
                raise _provider_stream_error(chunk_error)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            saw_choice = True
            choice = choices[0]
            delta = choice.delta
            content = getattr(delta, "content", None)
            if content:
                if stats["首Token耗时"] is None:
                    stats["首Token耗时"] = round(
                        (time.perf_counter() - started) * 1000
                    )
                    log_event(
                        current_context().module or "模型调用",
                        "收到首个Token",
                        details={"耗时": f"{stats['首Token耗时']}ms"},
                        progress=45,
                        progress_message="正在接收模型流式输出",
                    )
                content_chunks.append(content)
                stats["输出字符"] += len(content)

            for tool_call in (getattr(delta, "tool_calls", None) or []):
                index = tool_call.index
                entry = tool_chunks.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if tool_call.id:
                    entry["id"] = tool_call.id
                function = getattr(tool_call, "function", None)
                if function:
                    if function.name:
                        entry["name"] += function.name
                    if function.arguments:
                        entry["arguments"] += function.arguments
                        stats["工具参数字符"] += len(function.arguments)

            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except Exception as exc:
        try:
            setattr(exc, "_auto_eval_stream_stats", dict(stats))
        except Exception:
            pass
        raise
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    tool_calls = [
        AggregatedToolCall(
            id=entry["id"],
            type="function",
            function=AggregatedFunction(
                name=entry["name"], arguments=entry["arguments"]
            ),
        )
        for _, entry in sorted(tool_chunks.items())
    ] or None
    if not saw_choice:
        error = StreamProtocolError("流式响应中没有有效 choice")
        setattr(error, "_auto_eval_stream_stats", dict(stats))
        raise error

    normalized_finish_reason = str(finish_reason or "").strip().lower()
    if normalized_finish_reason in {
        "aborted",
        "abort",
        "cancelled",
        "canceled",
        "error",
        "failed",
        "server_error",
    }:
        error = ProviderStreamError(
            f"服务端异常结束生成：finish_reason={finish_reason}",
            body={"finish_reason": finish_reason},
            retriable=True,
        )
        setattr(error, "_auto_eval_stream_stats", dict(stats))
        raise error
    if normalized_finish_reason in {
        "blocked",
        "content_filter",
        "content_filtered",
        "safety",
        "prohibited_content",
    }:
        error = ProviderStreamError(
            f"服务端阻断生成：finish_reason={finish_reason}",
            body={"finish_reason": finish_reason},
            retriable=False,
        )
        setattr(error, "_auto_eval_stream_stats", dict(stats))
        raise error
    if not content_chunks and not tool_calls:
        error = ProviderStreamError(
            f"服务端返回空响应：finish_reason={finish_reason or '空'}",
            body={"finish_reason": finish_reason, "content": ""},
            retriable=True,
        )
        setattr(error, "_auto_eval_stream_stats", dict(stats))
        raise error

    response = AggregatedResponse(
        model=model,
        usage=usage,
        choices=[
            AggregatedChoice(
                index=0,
                finish_reason=finish_reason or "stop",
                message=AggregatedMessage(
                    content="".join(content_chunks),
                    tool_calls=tool_calls,
                ),
            )
        ],
    )
    return response, content_chunks, stats


async def stream_chat_completion(
    client,
    kwargs: dict,
    *,
    callback: Callable[[str], None] | None = None,
    include_usage: bool = True,
    total_timeout_s: float = 180.0,
    max_attempts: int = 4,
    retry_base_s: float = 1.0,
    retry_max_s: float = 20.0,
):
    """始终使用流式接口，成功后返回与完整响应等价的聚合对象。

    callback 在一次尝试完整成功后才收到分片，防止断流重试造成重复输出。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于等于 1")

    last_exc: BaseException | None = None
    use_usage = include_usage
    for attempt in range(max_attempts):
        module = current_context().module or "模型调用"
        call_started = time.perf_counter()
        log_event(
            module,
            "流式调用开始" if attempt == 0 else "开始重试",
            details={
                "模型": kwargs.get("model", ""),
                "请求次数": f"{attempt + 1}/{max_attempts}",
                "超时": f"{total_timeout_s:g}秒",
            },
            progress=35,
            progress_message=(
                f"{module}：等待模型响应"
                if attempt == 0
                else f"{module}：第{attempt + 1}次重试"
            ),
        )
        try:
            try:
                response, chunks, stats = await asyncio.wait_for(
                    _collect_stream(client, kwargs, include_usage=use_usage),
                    timeout=total_timeout_s,
                )
            except APIStatusError as exc:
                # 一些内部 OpenAI 兼容网关支持 stream，但不接受 stream_options。
                if use_usage and _rejects_stream_usage(exc):
                    use_usage = False
                    log_event(
                        module,
                        "网关不支持usage参数，降级重试",
                        level=logging.WARNING,
                        details={"HTTP状态": _status_code(exc)},
                    )
                    response, chunks, stats = await asyncio.wait_for(
                        _collect_stream(client, kwargs, include_usage=False),
                        timeout=total_timeout_s,
                    )
                else:
                    raise
            if callback is not None:
                for chunk in chunks:
                    try:
                        callback(chunk)
                    except Exception as callback_exc:
                        log_event(
                            module,
                            "流式回调失败",
                            level=logging.WARNING,
                            details=error_details(callback_exc, include_traceback=False),
                        )
            choice = response.choices[0]
            tool_calls = getattr(choice.message, "tool_calls", None) or []
            usage = getattr(response, "usage", None)
            log_event(
                module,
                "流式调用成功" if attempt == 0 else "重试成功",
                details={
                    "模型": kwargs.get("model", ""),
                    "请求次数": f"{attempt + 1}/{max_attempts}",
                    "结束原因": getattr(choice, "finish_reason", None),
                    "下一步": (
                        f"调用{tool_calls[0].function.name}" if tool_calls else "生成最终判定"
                    ),
                    "chunk数": stats.get("chunk数"),
                    "输出字符": stats.get("输出字符"),
                    "输入Token": getattr(usage, "prompt_tokens", None) if usage else None,
                    "输出Token": getattr(usage, "completion_tokens", None) if usage else None,
                    "耗时": f"{time.perf_counter() - call_started:.2f}秒",
                },
                progress=60,
                progress_message=(
                    f"{module}：模型返回成功"
                    if not tool_calls
                    else f"{module}：准备调用{tool_calls[0].function.name}"
                ),
            )
            return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            retriable = is_retriable_llm_error(exc)
            final = attempt >= max_attempts - 1 or not retriable
            stats = getattr(exc, "_auto_eval_stream_stats", {}) or {}
            details = {
                "模型": kwargs.get("model", ""),
                "请求次数": f"{attempt + 1}/{max_attempts}",
                "可重试": retriable,
                "已接收chunk": stats.get("chunk数"),
                "已接收字符": stats.get("输出字符"),
                "耗时": f"{time.perf_counter() - call_started:.2f}秒",
                **error_details(exc, include_traceback=final),
            }
            if final:
                log_event(
                    module,
                    (
                        "服务端返回错误，停止重试"
                        if isinstance(exc, ProviderStreamError)
                        else "流式调用最终失败"
                    ),
                    level=logging.ERROR,
                    details=details,
                    progress=100,
                    progress_message=f"{module}：模型调用失败",
                    progress_status="error",
                )
                raise
            cap = min(retry_max_s, retry_base_s * (2**attempt))
            wait = random.uniform(0.0, cap)
            details["等待"] = f"{wait:.2f}秒"
            log_event(
                module,
                (
                    "服务端返回错误，准备重试"
                    if isinstance(exc, ProviderStreamError)
                    else "调用失败，准备重试"
                ),
                level=logging.WARNING,
                details=details,
                progress=40,
                progress_message=f"{module}：调用失败，准备第{attempt + 2}次重试",
            )
            await asyncio.sleep(wait)

    assert last_exc is not None
    raise last_exc
