import json
from types import SimpleNamespace

import pytest

from auto_eval.judges.base import (
    JudgeClient,
    flush_web_trace_records,
)
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


def test_web_trace_is_buffered_until_full_result_is_available(tmp_path):
    path = tmp_path / "judge_calls.jsonl"
    client = _trace_client(path)
    pending = []

    with bind_chain_context(
        task_id="task",
        session_name="session",
        request_id="request",
        item_id="slow_query_001",
        item_index=0,
        judge_trace_callback=lambda trace_path, record: pending.append(
            (trace_path, record)
        ),
    ):
        client._write_trace({
            "status": "success",
            "judge": "judge_1",
            "llm_rounds": [
                {"round": 1, "content": "", "tool_calls": [{"name": "calculate"}]},
                {"round": 2, "content": "<analysis>raw</analysis>\n{\"total\":4}", "tool_calls": []},
            ],
        })

    assert not path.exists()
    result = {
        "index": 0,
        "item_id": "slow_query_001",
        "query": "打开设置",
        "context": "手机已解锁",
        "correctness": "right",
        "total": 4.5,
        "rubric": {"操作完成度": 5.0, "步骤正确性": 4.0},
        "rubric_reasons": {"步骤正确性": "有一次重试"},
        "arbitrated": False,
        "rationale": "关键帧显示目标设置已开启",
        "latency_s": 12.3,
        "category": "operation",
        "has_video": True,
    }

    assert flush_web_trace_records(pending, result) == 1
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["model_raw_output"] == "<analysis>raw</analysis>\n{\"total\":4}"
    assert record["query"] == "打开设置"
    assert record["correctness"] == "right"
    assert record["total"] == 4.5
    assert record["rubric"]["操作完成度"] == 5.0
    assert record["rubric_reasons"]["步骤正确性"] == "有一次重试"
    assert record["rationale"] == "关键帧显示目标设置已开启"
    assert record["latency_s"] == 12.3
    assert record["category"] == "operation"
    assert record["has_video"] is True
    assert "web_result" not in record


def test_web_result_fields_win_conflicts_without_losing_call_error(tmp_path):
    path = tmp_path / "judge_calls.jsonl"
    records = [(str(path), {
        "status": "error",
        "error_type": "APIError",
        "error": "provider failed",
        "llm_rounds": [],
    })]
    result = {
        "query": "q",
        "error_type": "judge_failed",
        "error": "评测失败",
        "index": 0,
    }

    assert flush_web_trace_records(records, result) == 1
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["error_type"] == "judge_failed"
    assert record["error"] == "评测失败"
    assert record["call_error_type"] == "APIError"
    assert record["call_error"] == "provider failed"
