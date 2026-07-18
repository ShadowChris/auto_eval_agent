import json
from types import SimpleNamespace

import pytest

from auto_eval.judges.base import JudgeClient
from auto_eval.observability import bind_chain_context


def _trace_client(path):
    client = object.__new__(JudgeClient)
    client.trace_path = str(path)
    client.cfg = SimpleNamespace(name="judge_1")
    client.model = "judge-model"
    return client


def test_judge_trace_contains_web_session_and_item_metadata(tmp_path):
    path = tmp_path / "judge_calls.jsonl"
    client = _trace_client(path)

    with bind_chain_context(
        task_id="aa5cd32001ec",
        session_name="20260717_103930_operation_aa5cd32001ec",
        request_id="2607171039_aa5cd3_q0",
        item_id="slow_query_001",
        item_index=0,
    ):
        client._write_trace({"status": "success", "judge": "judge_1"})

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["task_id"] == "aa5cd32001ec"
    assert record["session_name"] == "20260717_103930_operation_aa5cd32001ec"
    assert record["request_id"] == "2607171039_aa5cd3_q0"
    assert record["item_id"] == "slow_query_001"
    assert record["item_index"] == 0
    assert record["item_sequence"] == 1


@pytest.mark.asyncio
async def test_final_llm_failure_is_written_to_judge_trace(tmp_path):
    path = tmp_path / "judge_calls.jsonl"
    client = _trace_client(path)

    async def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    client._llm_create_stream = fail
    with bind_chain_context(
        task_id="task",
        session_name="session",
        request_id="request",
        item_id="item",
        item_index=2,
        round=3,
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await client._llm_create({"model": "judge-model", "messages": []})

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert record["error_type"] == "RuntimeError"
    assert record["error"] == "provider unavailable"
    assert record["round"] == 3
    assert record["item_sequence"] == 3
