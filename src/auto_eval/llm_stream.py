"""OpenAI 兼容接口的流式调用、完整响应聚合与重试。"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError


class StreamProtocolError(RuntimeError):
    """流正常关闭，但没有返回可用的 choice。"""


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

    try:
        async for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            model = getattr(chunk, "model", None) or model
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            saw_choice = True
            choice = choices[0]
            delta = choice.delta
            content = getattr(delta, "content", None)
            if content:
                content_chunks.append(content)

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

            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    if not saw_choice:
        raise StreamProtocolError("流式响应中没有有效 choice")

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
    return response, content_chunks


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
        try:
            try:
                response, chunks = await asyncio.wait_for(
                    _collect_stream(client, kwargs, include_usage=use_usage),
                    timeout=total_timeout_s,
                )
            except APIStatusError as exc:
                # 一些内部 OpenAI 兼容网关支持 stream，但不接受 stream_options。
                if use_usage and _rejects_stream_usage(exc):
                    use_usage = False
                    response, chunks = await asyncio.wait_for(
                        _collect_stream(client, kwargs, include_usage=False),
                        timeout=total_timeout_s,
                    )
                else:
                    raise
            if callback is not None:
                for chunk in chunks:
                    try:
                        callback(chunk)
                    except Exception:
                        pass
            return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts - 1 or not is_retriable_llm_error(exc):
                raise
            cap = min(retry_max_s, retry_base_s * (2**attempt))
            await asyncio.sleep(random.uniform(0.0, cap))

    assert last_exc is not None
    raise last_exc
