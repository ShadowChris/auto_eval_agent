import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

from auto_eval.config import JudgeConfig, load_config
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.observability import current_context
from auto_eval.web import history, runner, server
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


def test_runner_snapshot_persistence_is_throttled_but_forceable(monkeypatch):
    task = Task(id="persist-throttle", mode="single", items=[], options={})
    saved: list[str] = []
    times = iter([10.0, 10.2, 10.3])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        runner,
        "save_task",
        lambda current: saved.append(current.id) or True,
    )

    assert runner._persist_task(task) is True
    assert runner._persist_task(task) is True
    assert runner._persist_task(task, force=True) is True

    assert saved == [task.id, task.id]


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


def test_running_snapshot_exposes_explicit_total_progress():
    task = Task(
        id="running-progress",
        mode="operation",
        items=[
            {"id": "q0", "query": "q0"},
            {"id": "q1", "query": "q1"},
            {"id": "q2", "query": "q2"},
        ],
        options={},
        status="running",
        results=[{"index": 0, "item_id": "q0", "correctness": "ok"}],
        # 兼容早期快照中 done_total 未及时刷新的情况。
        done_total=0,
    )

    payload = history.snapshot_payload(history.task_to_snapshot(task))

    assert payload["status"] == "running"
    assert payload["done_total"] == 1
    assert payload["total"] == 3


def test_compact_snapshot_omits_heavy_item_media_and_limits_progress_events():
    task = Task(
        id="compact-history",
        mode="operation",
        items=[{
            "id": "q1",
            "query": "打开设置",
            "context": "背景",
            "video_path": "/large/video.mp4",
            "frames": [f"frame-{index}.jpg" for index in range(30)],
            "source_data": {"大字段": "x" * 1000},
        }],
        options={},
        event_cursor=42,
        progress_events={
            "0": [
                {"item_index": 0, "sequence": index}
                for index in range(30)
            ],
        },
    )

    payload = history.snapshot_payload(
        history.task_to_snapshot(task),
        compact=True,
    )

    assert payload["items"] == [{"id": "q1", "query": "打开设置", "context": "背景"}]
    assert len(payload["progress_events"]["0"]) == 2
    assert payload["progress_events"]["0"][0]["sequence"] == 28
    assert payload["event_cursor"] == 42


@pytest.mark.asyncio
async def test_stream_replays_task_level_progress_before_item_events(monkeypatch):
    task = Task(
        id="stream-progress",
        mode="operation",
        items=[{"query": f"q{index}"} for index in range(10)],
        options={},
        status="running",
        results=[{"index": 0, "item_id": "q0", "correctness": "ok"}],
        done_total=1,
    )
    monkeypatch.setattr(server, "get_task", lambda _: task)

    response = await server.api_stream(task.id)
    first_chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()
    text = first_chunk.decode() if isinstance(first_chunk, bytes) else first_chunk

    assert "event: task_state" in text
    assert '"status": "running"' in text
    assert '"progress": 1' in text
    assert '"total": 10' in text


@pytest.mark.asyncio
async def test_stream_with_cursor_only_replays_incremental_events(monkeypatch):
    task = Task(
        id="stream-cursor",
        mode="operation",
        items=[{"query": "q0"}],
        options={},
        status="running",
    )
    task.publish_nowait("progress_event", {"item_index": 0, "sequence": 1})
    task.publish_nowait("item_progress", {"item_index": 0, "sequence": 2})
    task.publish_nowait("result", {
        "progress": 1,
        "total": 1,
        "result": {"index": 0, "query": "q0"},
    })
    monkeypatch.setattr(server, "get_task", lambda _: task)

    response = await server.api_stream(task.id, after=2, last_event_id=None)
    state_chunk = await anext(response.body_iterator)
    result_chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()
    state_text = state_chunk.decode() if isinstance(state_chunk, bytes) else state_chunk
    result_text = result_chunk.decode() if isinstance(result_chunk, bytes) else result_chunk

    assert "event: task_state" in state_text
    assert "event: progress_event" not in result_text
    assert "event: item_progress" not in result_text
    assert "id: 3" in result_text
    assert "event: result" in result_text
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"


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
        {"id": "case-1", "query": "q", "context": "c"},
        error,
        request_id="2607052331_218ba6_q1",
    )

    record = json.loads((tmp_path / "eval_errors.jsonl").read_text(encoding="utf-8"))
    assert record["request_id"] == "2607052331_218ba6_q1"
    assert record["item_id"] == "case-1"
    assert record["query"] == "q"
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
        items=[{"id": "case-17", "query": "q", "answer": "a"}],
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
    assert task.results[0]["item_id"] == "case-17"
    assert task.results[0]["query"] == "q"
    assert "ValueError" in task.results[0]["error"]


def test_export_backfills_identity_for_historical_failed_results():
    snapshot = {
        "task_id": "historical-failure",
        "mode": "operation",
        "items": [
            {"id": "fast_query_4", "query": "设置为静音"},
            {"query": "打开24小时制"},
        ],
        "results": [
            {"index": 0, "query": "设置为静音", "error": "provider failed"},
            {"index": 1, "error": "video missing"},
        ],
        "summary": {},
    }

    sheets = history.export_rows(snapshot)

    assert "评估失败" not in sheets
    assert [row["数据集序号"] for row in sheets["逐题结果"]] == [1, 2]
    assert [row["item_id"] for row in sheets["逐题结果"]] == ["fast_query_4", "q1"]
    assert [row["query"] for row in sheets["逐题结果"]] == ["设置为静音", "打开24小时制"]
    assert [row["评估状态"] for row in sheets["逐题结果"]] == ["评估失败", "评估失败"]


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
            "video_prepare_warnings": [
                "task_end_time=12 秒超出视频时长 8.5 秒，已忽略该值并使用默认结束时间"
            ],
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
            "correctness": "ok",
            "issue_types": [],
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
    assert task.results[0]["video_prepare_warnings"] == [
        "task_end_time=12 秒超出视频时长 8.5 秒，已忽略该值并使用默认结束时间"
    ]
    assert any(
        event.get("event") == "时间参数异常，已回退默认值"
        for event in task.progress_events["0"]
    )
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

    event_queue = task.subscribe()
    await runner.run_eval(task, SimpleNamespace())

    events = []
    while not event_queue.empty():
        events.append((await event_queue.get())["event"])
    task.unsubscribe(event_queue)
    assert events == ["start", "result", "done"]
    assert task.status == "done"
    assert task.error is None
    assert task.results[0]["error"] == "simulated model failure"


@pytest.mark.asyncio
async def test_task_events_are_broadcast_to_each_sse_subscriber():
    task = Task(id="broadcast", mode="single", items=[], options={})
    first = task.subscribe()
    second = task.subscribe()

    await task.publish("item_progress", {"item_index": 0, "percent": 20})

    assert await first.get() == {
        "event": "item_progress",
        "data": {"item_index": 0, "percent": 20},
        "cursor": 1,
    }
    assert await second.get() == {
        "event": "item_progress",
        "data": {"item_index": 0, "percent": 20},
        "cursor": 1,
    }
    assert task.event_cursor == 1
    assert task.event_log[-1]["cursor"] == 1
    task.unsubscribe(first)
    task.unsubscribe(second)
    assert not task.subscribers


@pytest.mark.asyncio
async def test_cancel_running_task_persists_partial_results_and_notifies_subscribers(monkeypatch):
    task = Task(
        id="cancel-running",
        mode="single",
        items=[{"id": "q0", "query": "done"}, {"id": "q1", "query": "pending"}],
        options={},
        status="running",
        results=[{"index": 0, "item_id": "q0", "query": "done"}],
        done_total=1,
        item_progress={"0": {"item_index": 0, "status": "done", "percent": 100}},
    )
    execution = asyncio.create_task(asyncio.sleep(60))
    task.execution = execution
    events = task.subscribe()
    saved = []
    monkeypatch.setattr(server, "get_live_task", lambda task_id: task if task_id == task.id else None)
    monkeypatch.setattr(server, "save_task", lambda current: saved.append(current.status) or True)

    response = await server.api_eval_cancel(task.id)
    await asyncio.sleep(0)

    assert response["status"] == "cancelled"
    assert task.status == "cancelled"
    assert task.done_total == 1
    assert task.results == [{"index": 0, "item_id": "q0", "query": "done"}]
    assert task.item_progress["0"]["status"] == "done"
    assert task.item_progress["1"]["status"] == "cancelled"
    assert saved == ["cancelled"]
    assert execution.cancelled()
    emitted = []
    while not events.empty():
        emitted.append((await events.get())["event"])
    assert emitted == ["item_progress", "cancelled"]
    task.unsubscribe(events)
    with suppress(asyncio.CancelledError):
        await execution


def test_history_api_keeps_live_running_status(monkeypatch):
    task = Task(
        id="live-history",
        mode="operation",
        items=[{"query": "q1"}, {"query": "q2"}],
        options={},
        status="running",
        done_total=1,
    )
    monkeypatch.setattr(server, "list_snapshots", lambda limit: [{
        "task_id": task.id,
        "status": "error",
        "done": 0,
        "total": 2,
        "error": "服务中断",
    }])
    monkeypatch.setattr(server, "get_live_task", lambda task_id: task if task_id == task.id else None)

    row = server.api_history(limit=50)["items"][0]

    assert row["status"] == "running"
    assert row["done"] == 1
    assert row["error"] is None


def test_history_api_returns_server_paginated_total(monkeypatch):
    monkeypatch.setattr(
        server,
        "list_snapshots_page",
        lambda page, page_size: ([{
            "task_id": "page-task",
            "status": "done",
            "done": 1,
            "total": 1,
        }], 37),
    )
    monkeypatch.setattr(server, "get_live_task", lambda _: None)

    payload = server.api_history(page=2, page_size=10)

    assert payload["total"] == 37
    assert payload["page"] == 2
    assert payload["page_size"] == 10
    assert payload["items"][0]["task_id"] == "page-task"
