import asyncio
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.llm_stream import stream_chat_completion


def _chunk(content=None, *, finish=None, tool_calls=None, usage=None):
    choices = []
    if content is not None or finish is not None or tool_calls:
        choices = [
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish,
            )
        ]
    return SimpleNamespace(
        choices=choices,
        usage=usage,
        model="fake-model",
    )


def _tool_chunk(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeStream:
    def __init__(self, events, delay=0):
        self.events = events
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        attempt = self.attempts.pop(0)
        if isinstance(attempt, BaseException):
            raise attempt
        return FakeStream(attempt)


def _client(attempts):
    completions = FakeCompletions(attempts)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    ), completions


@pytest.mark.asyncio
async def test_stream_aggregates_content_usage_and_tool_calls():
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)
    client, completions = _client(
        [[
            _chunk("你"),
            _chunk("好"),
            _chunk(
                tool_calls=[
                    _tool_chunk(0, call_id="call-1", name="web_", arguments='{"q":')
                ]
            ),
            _chunk(
                finish="tool_calls",
                tool_calls=[_tool_chunk(0, name="search", arguments='"x"}')],
            ),
            _chunk(usage=usage),
        ]]
    )
    callback_chunks = []

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        callback=callback_chunks.append,
        max_attempts=1,
    )

    request = completions.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert response.choices[0].message.content == "你好"
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage is usage
    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.id == "call-1"
    assert tool_call.function.name == "web_search"
    assert tool_call.function.arguments == '{"q":"x"}'
    assert callback_chunks == ["你", "好"]


@pytest.mark.asyncio
async def test_stream_retry_discards_partial_callback_output():
    client, completions = _client(
        [
            [_chunk("重复内容"), httpx.RemoteProtocolError("peer closed")],
            [_chunk("完"), _chunk("整", finish="stop")],
        ]
    )
    callback_chunks = []

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        callback=callback_chunks.append,
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == "完整"
    assert callback_chunks == ["完", "整"]


@pytest.mark.asyncio
async def test_stream_total_timeout():
    completions = FakeCompletions([])

    async def create(**kwargs):
        completions.requests.append(kwargs)
        return FakeStream([_chunk("太慢")], delay=0.05)

    completions.create = create
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(asyncio.TimeoutError):
        await stream_chat_completion(
            client,
            {"model": "fake-model", "messages": []},
            total_timeout_s=0.01,
            max_attempts=1,
        )
