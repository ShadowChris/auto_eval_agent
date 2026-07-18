import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

from auto_eval.config import JudgeConfig, load_config
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.observability import current_context
from auto_eval.web import history, runner
from auto_eval.web.server import EvalReq, _validate_eval_request
from auto_eval.web.tasks import Task


def _judge_config(*judges):
    return SimpleNamespace(judges=list(judges))


def test_product_expert_only_requires_competitor():
    req = EvalReq(
        mode="single",
        items=[{"query": "q", "answer": "a"}],
        options={"judges": ["product"]},
    )
    config = _judge_config(
        JudgeConfig(name="product", persona="product_expert"),
        JudgeConfig(name="user", persona="end_user"),
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_eval_request(req, config)

    assert exc_info.value.status_code == 422
    assert "competitor" in exc_info.value.detail


def test_product_expert_with_competitor_is_allowed():
    req = EvalReq(
        mode="single",
        items=[{"query": "q", "answer": "a", "competitor": "b"}],
        options={"judges": ["product"]},
    )
    config = _judge_config(JudgeConfig(name="product", persona="product_expert"))

    _validate_eval_request(req, config)


def test_mixed_judges_without_competitor_is_allowed():
    req = EvalReq(
        mode="single",
        items=[{"query": "q", "answer": "a"}],
        options={"judges": ["product", "user"]},
    )
    config = _judge_config(
        JudgeConfig(name="product", persona="product_expert"),
        JudgeConfig(name="user", persona="end_user"),
    )

    _validate_eval_request(req, config)


def test_save_task_retries_transient_replace_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    real_replace = history.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary Windows file lock")
        real_replace(source, target)

    monkeypatch.setattr(history.os, "replace", flaky_replace)
    task = Task(id="retry-save", mode="single", items=[], options={})

    assert history.save_task(task, max_attempts=3) is True
    assert attempts == 3
    assert (tmp_path / "retry-save.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_save_task_failure_is_non_fatal_and_cleans_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)

    def always_fail(source, target):
        raise PermissionError("locked")

    monkeypatch.setattr(history.os, "replace", always_fail)
    task = Task(id="failed-save", mode="single", items=[], options={})

    assert history.save_task(task, max_attempts=2) is False
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_history_is_bounded_and_keeps_multi_judge_events():
    task = Task(id="multi-progress", mode="single", items=[], options={})

    for index in range(105):
        judge = "研发人员(judge_1)" if index % 2 == 0 else "终端用户(judge_2)"
        runner._record_progress(
            task,
            0,
            {
                "item_index": 0,
                "request_id": "2607051200_multi_q0",
                "judge": judge,
                "round": index + 1,
                "module": "模型裁判",
                "message": f"event-{index + 1}",
                "status": "running",
                "percent": 40,
            },
        )

    events = task.progress_events["0"]
    assert len(events) == runner.MAX_PROGRESS_EVENTS_PER_ITEM
    assert events[0]["sequence"] == 6
    assert events[-1]["sequence"] == 105
    assert {event["judge"] for event in events} == {
        "研发人员(judge_1)",
        "终端用户(judge_2)",
    }
    assert task.item_progress["0"] == events[-1]


def test_progress_keeps_started_at_after_evaluation_begins():
    task = Task(id="progress-timer", mode="single", items=[], options={})

    queued = runner._record_progress(
        task,
        0,
        {"item_index": 0, "status": "pending", "message": "排队等待评测"},
    )
    assert "started_at" not in queued

    started = runner._record_progress(
        task,
        0,
        {
            "item_index": 0,
            "status": "running",
            "message": "开始评测",
            "started_at": 1_788_517_600_000,
        },
    )
    assert started["started_at"] == 1_788_517_600_000

    completed = runner._record_progress(
        task,
        0,
        {"item_index": 0, "status": "done", "message": "评测完成"},
    )
    assert completed["started_at"] == 1_788_517_600_000


def test_eval_error_keeps_original_and_repaired_model_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    error = JudgeOutputParseError(
        "裁判输出定向修复后仍无法解析为 JSON",
        raw_output='{"rubric":{"准确性":5}',
        repair_output='{"rubric":{"准确性":5}} trailing',
        judge="judge_2",
        model="fake-model",
    )

    runner._write_eval_error(
        "task-1",
        1,
        {"query": "q", "context": "c"},
        error,
        request_id="2607052331_218ba6_q1",
    )

    record = json.loads((tmp_path / "eval_errors.jsonl").read_text(encoding="utf-8"))
    assert record["request_id"] == "2607052331_218ba6_q1"
    assert record["stage"] == "judge_json_parse"
    assert record["original_model_output"] == error.raw_output
    assert record["repair_model_output"] == error.repair_output
    assert record["original_output_length"] == len(error.raw_output)
    assert record["repair_output_length"] == len(error.repair_output)


@pytest.mark.asyncio
async def test_non_retriable_parse_error_does_not_restart_whole_item(monkeypatch):
    cfg = load_config(Path("config"))
    task = Task(
        id="parse-error-no-restart",
        mode="single",
        items=[{"query": "q", "answer": "a"}],
        options={"judges": [cfg.judges[0].name], "concurrency": 1},
    )
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("裁判输出定向修复后仍无法解析为 JSON")

    monkeypatch.setattr(runner, "_eval_one", fail_once)
    monkeypatch.setattr(runner, "_persist_task", lambda task: True)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)

    await runner._run(task, cfg)

    assert calls == 1
    assert len(task.results) == 1
    assert "ValueError" in task.results[0]["error"]


@pytest.mark.asyncio
async def test_operation_video_is_prepared_only_when_evaluation_starts(
    tmp_path, monkeypatch
):
    cfg = load_config(Path("config"))
    task = Task(
        id="operation-session",
        mode="operation",
        items=[{
            "id": "slow_query_001",
            "query": "打开设置",
            "video_path": "data/slow_query_001.mp4",
        }],
        options={"judges": [cfg.judges[0].name], "concurrency": 1},
        session_name="20260717_103930_operation_operation-session",
    )
    prepare_calls = []
    trace_path = tmp_path / "judge_calls.jsonl"

    def fake_prepare(item, **kwargs):
        prepare_calls.append(kwargs)
        return {
            **item,
            "video_path": "/abs/slow_query_001.mp4",
            "media": ["/abs/slow_query_001.mp4"],
            "frames": ["/abs/session/001_slow_query_001/kf_001.jpg"],
            "frame_count": 1,
            "duration": 8.5,
        }

    async def fake_eval(*args, **kwargs):
        assert task.items[0]["frames"] == ["/abs/session/001_slow_query_001/kf_001.jpg"]
        current_context().judge_trace_callback(str(trace_path), {
            "task_id": task.id,
            "session_name": task.session_name,
            "request_id": current_context().request_id,
            "item_id": "slow_query_001",
            "item_index": 0,
            "status": "success",
            "judge": "judge_1",
            "llm_rounds": [{"round": 1, "content": "raw model output"}],
        })
        return {
            "index": 0,
            "item_id": "slow_query_001",
            "query": "打开设置",
            "correctness": "right",
            "total": 5,
            "rubric": {"操作完成度": 5},
            "rationale": "操作已完成",
            "latency_s": 1.2,
        }

    monkeypatch.setattr(runner, "prepare_session_operation_item", fake_prepare)
    monkeypatch.setattr(runner, "_eval_one", fake_eval)
    monkeypatch.setattr(runner, "_persist_task", lambda task: True)

    await runner._run(task, cfg)

    assert len(prepare_calls) == 1
    assert prepare_calls[0]["session_name"] == task.session_name
    assert prepare_calls[0]["item_index"] == 0
    assert prepare_calls[0]["total_items"] == 1
    assert task.results[0]["total"] == 5
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["model_raw_output"] == "raw model output"
    for field, value in task.results[0].items():
        assert trace[field] == value


@pytest.mark.asyncio
async def test_snapshot_exception_does_not_replace_result_with_global_error(monkeypatch):
    task = Task(
        id="snapshot-error",
        mode="single",
        items=[{"query": "q", "answer": "a"}],
        options={},
    )

    async def fake_run(current_task, _cfg):
        result = {"index": 0, "query": "q", "error": "simulated model failure"}
        current_task.results.append(result)
        current_task.done_total = 1
        await current_task.publish(
            "result",
            {"progress": 1, "total": 1, "result": result},
        )

    def broken_save(_task):
        raise PermissionError("snapshot locked")

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_summarize", lambda _task, _cfg: {"failed": 1})
    monkeypatch.setattr(runner, "save_task", broken_save)

    await runner.run_eval(task, SimpleNamespace())

    events = []
    while not task.queue.empty():
        events.append((await task.queue.get())["event"])
    assert events == ["start", "result", "done"]
    assert task.status == "done"
    assert task.error is None
    assert task.results[0]["error"] == "simulated model failure"
