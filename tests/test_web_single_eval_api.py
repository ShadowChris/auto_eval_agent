import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auto_eval.web import runner, server
from auto_eval.web.server import SingleEvalReq
from auto_eval.web.tasks import Task


@pytest.fixture(autouse=True)
def _default_single_eval_concurrency(monkeypatch):
    monkeypatch.delenv("AUTO_EVAL_SINGLE_CONCURRENCY", raising=False)


def _config():
    return SimpleNamespace(
        judges=[
            SimpleNamespace(
                name="judge_1",
                display="研发人员",
                persona="strict_expert",
            ),
            SimpleNamespace(
                name="judge_2",
                display="终端用户",
                persona="end_user",
            ),
        ],
    )


def _item(item_id: str = "simple_001") -> dict:
    return {
        "id": item_id,
        "query": "关闭定位",
        "answer": "已帮你关闭定位",
        "context": "测试手机",
        "video_path": "data/videos/simple_001.mp4",
        "task_start_time": 1,
        "task_end_time": 12.5,
        "session_id": "session-1",
        "custom_field": "保留",
    }


def test_single_eval_concurrency_uses_environment_with_safe_bounds(monkeypatch):
    assert server._single_eval_concurrency() == 15
    monkeypatch.setenv("AUTO_EVAL_SINGLE_CONCURRENCY", "20")
    assert server._single_eval_concurrency() == 20
    monkeypatch.setenv("AUTO_EVAL_SINGLE_CONCURRENCY", "0")
    assert server._single_eval_concurrency() == 1
    monkeypatch.setenv("AUTO_EVAL_SINGLE_CONCURRENCY", "invalid")
    assert server._single_eval_concurrency() == 15


def _completed_task() -> Task:
    return Task(
        id="api_dataset_001",
        mode="operation",
        dataset_name="接口测试集",
        items=[{
            "id": "simple_001",
            "query": "关闭定位",
            "context": "测试手机",
            "answer": "已关闭定位",
            "video_path": "data/videos/simple_001.mp4",
            "source_data": {
                "id": "simple_001",
                "query": "关闭定位",
                "context": "测试手机",
                "answer": "已关闭定位",
                "video_path": "data/videos/simple_001.mp4",
                "session_id": "session-1",
                "分享链接": "https://example.com/1",
            },
        }],
        options={"submission_source": "single_api"},
        status="done",
        results=[{
            "index": 0,
            "item_id": "simple_001",
            "query": "关闭定位",
            "task_type": "simple",
            "execution_routes": ["fast_system", "jarvis"],
            "route_status": "detected",
            "route_evidence": [{
                "route": "fast_system",
                "evidence_frames": [5, 6],
                "evidence": "状态卡显示定位已关闭",
                "confidence": 0.96,
            }],
            "route_rationale": "先走快系统，后进入操控链路",
            "correctness": "ok",
            "issue_types": [],
            "is_low_level": "no",
            "total": 5,
            "rubric": {"操作完成度": 5, "步骤正确性": 5},
            "rubric_reasons": {
                "操作完成度": "任务完成",
                "步骤正确性": "路径正确",
            },
            "rationale": "任务完成闭环",
            "latency_s": 12.3,
        }],
        item_progress={
            "0": {"item_index": 0, "status": "done", "percent": 100},
        },
        progress_events={
            "0": [
                {"item_index": 0, "sequence": 1, "message": "等待评估"},
                {"item_index": 0, "sequence": 2, "message": "正在抽帧"},
                {"item_index": 0, "sequence": 3, "message": "评测完成"},
            ],
        },
    )


def test_single_eval_result_uses_excel_mapping_and_json_route_evidence(monkeypatch):
    task = _completed_task()
    monkeypatch.setattr(server, "get_task", lambda task_id: task)

    response = server.api_eval_single_result(task.id, "simple_001", event=0)

    assert response["evaluation_status"] == "succeeded"
    assert "progress_events" not in response
    result = response["result"]
    assert result["item_id"] == "simple_001"
    assert result["sessionid"] == "session-1"
    assert result["链路类型"] == "快系统；贾维斯"
    assert result["execution_routes"] == "fast_system；jarvis"
    assert result["route_evidence"] == [{
        "route": "fast_system",
        "evidence_frames": [5, 6],
        "evidence": "状态卡显示定位已关闭",
        "confidence": 0.96,
        "route_name": "快系统",
    }]
    assert result["维度_操作完成度"] == 5
    assert result["理由_步骤正确性"] == "路径正确"
    assert result["评估状态"] == "已完成"


def test_single_eval_post_and_get_share_one_route():
    operation = server.app.openapi()["paths"]["/api/eval/single"]
    assert {"get", "post"}.issubset(operation)
    parameters = {
        (parameter["name"], parameter["in"])
        for parameter in operation["get"]["parameters"]
    }
    assert ("task_id", "query") in parameters
    assert ("id", "query") in parameters
    assert ("event", "query") in parameters
    assert "/api/eval/single/{task_id}/{id}" not in server.app.openapi()["paths"]


def test_single_eval_result_event_controls_progress_events(monkeypatch):
    task = _completed_task()
    monkeypatch.setattr(server, "get_task", lambda task_id: task)

    first_two = server.api_eval_single_result(task.id, "simple_001", event=2)
    assert [row["sequence"] for row in first_two["progress_events"]] == [1, 2]

    all_events = server.api_eval_single_result(task.id, "simple_001", event=-1)
    assert [row["sequence"] for row in all_events["progress_events"]] == [1, 2, 3]

    more_than_available = server.api_eval_single_result(
        task.id,
        "simple_001",
        event=10,
    )
    assert len(more_than_available["progress_events"]) == 3


def test_single_eval_result_rejects_invalid_event_or_identity(monkeypatch):
    task = _completed_task()
    monkeypatch.setattr(server, "get_task", lambda task_id: task)

    with pytest.raises(HTTPException) as event_error:
        server.api_eval_single_result(task.id, "simple_001", event=-2)
    assert event_error.value.status_code == 422

    with pytest.raises(HTTPException) as item_error:
        server.api_eval_single_result(task.id, "missing", event=0)
    assert item_error.value.status_code == 404

    monkeypatch.setattr(server, "get_task", lambda task_id: None)
    with pytest.raises(HTTPException) as task_error:
        server.api_eval_single_result(task.id, "simple_001", event=0)
    assert task_error.value.status_code == 404

    other_mode = _completed_task()
    other_mode.mode = "single"
    monkeypatch.setattr(server, "get_task", lambda task_id: other_mode)
    with pytest.raises(HTTPException) as mode_error:
        server.api_eval_single_result(other_mode.id, "simple_001", event=0)
    assert mode_error.value.status_code == 409


def test_single_eval_result_returns_mapped_rows_while_pending_or_failed(monkeypatch):
    task = _completed_task()
    task.results = []
    task.status = "running"
    task.item_progress["0"] = {"status": "pending", "message": "等待评估"}
    monkeypatch.setattr(server, "get_task", lambda task_id: task)

    pending = server.api_eval_single_result(task.id, "simple_001", event=0)
    assert pending["evaluation_status"] == "pending"
    assert pending["result"]["评估状态"] == "待评估"
    assert pending["result"]["route_evidence"] == []

    task.results = [{
        "index": 0,
        "item_id": "simple_001",
        "query": "关闭定位",
        "error": "视频文件不存在",
    }]
    failed = server.api_eval_single_result(task.id, "simple_001", event=0)
    assert failed["evaluation_status"] == "failed"
    assert failed["result"]["评估状态"] == "评估失败"
    assert failed["result"]["error"] == "视频文件不存在"


def test_single_item_evaluation_status_uses_result_and_progress():
    task = _completed_task()
    assert server._single_item_evaluation_status(task, 0) == "succeeded"

    task.results[0]["error"] = "model failed"
    assert server._single_item_evaluation_status(task, 0) == "failed"

    task.results = []
    task.item_progress["0"] = {"status": "running"}
    assert server._single_item_evaluation_status(task, 0) == "running"

    task.item_progress["0"] = {"status": "pending"}
    assert server._single_item_evaluation_status(task, 0) == "pending"

    task.status = "cancelled"
    assert server._single_item_evaluation_status(task, 0) == "cancelled"


@pytest.mark.asyncio
async def test_single_eval_api_creates_operation_task_with_end_user(monkeypatch):
    created: dict = {}

    def fake_new_task(mode, items, options, dataset_name="", *, task_id=None):
        task = Task(
            id=task_id,
            mode=mode,
            items=items,
            options=options,
            dataset_name=dataset_name,
        )
        created["task"] = task
        return task

    async def fake_run_single(task, app_cfg, item_index, item_id, *, rerun=None):
        task.item_executions.pop(item_id, None)
        task.status = "done"

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "get_task", lambda task_id: None)
    monkeypatch.setattr(server, "new_task", fake_new_task)
    monkeypatch.setattr(server, "run_single_api_item", fake_run_single)

    response = await server.api_eval_single(SingleEvalReq(
        task_id="api_dataset_001",
        dataset_name="接口冒烟集",
        item=_item(),
    ))

    task = created["task"]
    assert response == {
        "task_id": "api_dataset_001",
        "id": "simple_001",
        "status": "success",
        "evaluation_status": "running",
        "action": "created",
        "dataset_size": 1,
    }
    assert task.mode == "operation"
    assert task.dataset_name == "接口冒烟集"
    assert task.options == {
        "judges": ["judge_2"],
        "concurrency": 15,
        "submission_source": "single_api",
    }
    assert task.items[0]["id"] == "simple_001"
    assert task.items[0]["source_data"]["custom_field"] == "保留"
    assert task.items[0]["source_data"]["session_id"] == "session-1"

    assert task.item_executions["simple_001"] is not None
    execution = task.item_executions["simple_001"]
    await execution
    await asyncio.sleep(0)
    assert task.status == "done"


@pytest.mark.asyncio
async def test_single_eval_api_overwrites_only_matching_item(monkeypatch):
    task = Task(
        id="api_dataset_001",
        mode="operation",
        items=[
            {"id": "simple_001", "query": "旧问题", "video_path": "old.mp4"},
            {"id": "simple_002", "query": "保持不变", "video_path": "keep.mp4"},
        ],
        options={"judges": ["judge_1"], "concurrency": 8},
        dataset_name="旧名称",
        status="done",
        results=[
            {"index": 0, "item_id": "simple_001", "correctness": "nok"},
            {"index": 1, "item_id": "simple_002", "correctness": "ok"},
        ],
    )
    executed: dict = {}

    async def fake_run_single(current, app_cfg, item_index, item_id, *, rerun=None):
        executed.update({
            "task_id": current.id,
            "index": item_index,
            "item_id": item_id,
            "rerun": rerun,
        })
        current.item_executions.pop(item_id, None)
        current.status = "done"

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "get_task", lambda task_id: task)
    monkeypatch.setattr(server, "save_task", lambda current: True)
    monkeypatch.setattr(server, "run_single_api_item", fake_run_single)

    response = await server.api_eval_single(SingleEvalReq(
        task_id=task.id,
        dataset_name="新名称",
        item=_item("simple_001"),
    ))

    assert response["action"] == "overwritten"
    assert response["evaluation_status"] == "running"
    assert task.items[0]["query"] == "关闭定位"
    assert task.items[1]["query"] == "保持不变"
    assert task.results == [
        {"index": 1, "item_id": "simple_002", "correctness": "ok"},
    ]
    assert task.done_total == 1
    assert task.dataset_name == "新名称"
    assert task.options["judges"] == ["judge_2"]
    assert task.options["concurrency"] == 15
    execution = task.item_executions["simple_001"]
    await execution
    assert executed["task_id"] == task.id
    assert executed["index"] == 0
    assert executed["item_id"] == "simple_001"
    assert executed["rerun"]["attempt_no"] == 1
    assert executed["rerun"]["item_indices"] == [0]
    assert executed["rerun"]["_previous_result"]["correctness"] == "nok"


@pytest.mark.asyncio
async def test_single_eval_api_appends_new_item_and_reuses_rerun(monkeypatch):
    task = Task(
        id="api_dataset_001",
        mode="operation",
        items=[{"id": "simple_001", "query": "旧问题"}],
        options={"judges": ["judge_2"]},
        status="done",
    )
    executed_indices: list[int] = []

    async def fake_run_single(current, app_cfg, item_index, item_id, *, rerun=None):
        executed_indices.append(item_index)
        assert rerun is None
        current.item_executions.pop(item_id, None)
        current.status = "done"

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "get_task", lambda task_id: task)
    monkeypatch.setattr(server, "save_task", lambda current: True)
    monkeypatch.setattr(server, "run_single_api_item", fake_run_single)

    response = await server.api_eval_single(SingleEvalReq(
        task_id=task.id,
        item=_item("simple_002"),
    ))

    assert response["action"] == "appended"
    assert response["dataset_size"] == 2
    assert [item["id"] for item in task.items] == ["simple_001", "simple_002"]
    await task.item_executions["simple_002"]
    assert executed_indices == [1]


@pytest.mark.asyncio
async def test_single_eval_api_allows_different_ids_in_parallel_but_rejects_same_id(monkeypatch):
    task = Task(
        id="api_dataset_001",
        mode="operation",
        items=[{"id": "simple_001", "query": "正在评估"}],
        options={
            "judges": ["judge_2"],
            "concurrency": 4,
            "submission_source": "single_api",
        },
        status="running",
    )
    release = asyncio.Event()

    async def existing_run():
        await release.wait()

    existing_execution = asyncio.create_task(existing_run())
    task.item_executions["simple_001"] = existing_execution

    async def fake_run_single(current, app_cfg, item_index, item_id, *, rerun=None):
        await release.wait()
        current.item_executions.pop(item_id, None)

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "get_task", lambda task_id: task)
    monkeypatch.setattr(server, "save_task", lambda current: True)
    monkeypatch.setattr(server, "run_single_api_item", fake_run_single)

    try:
        response = await server.api_eval_single(SingleEvalReq(
            task_id=task.id,
            item=_item("simple_002"),
        ))
        assert response["action"] == "appended"
        assert set(task.item_executions) == {"simple_001", "simple_002"}

        with pytest.raises(HTTPException) as same_item_error:
            await server.api_eval_single(SingleEvalReq(
                task_id=task.id,
                item=_item("simple_001"),
            ))
        assert same_item_error.value.status_code == 409
        assert "simple_001" in str(same_item_error.value.detail)
    finally:
        second_execution = task.item_executions.get("simple_002")
        release.set()
        await existing_execution
        if second_execution is not None:
            await second_execution


@pytest.mark.asyncio
async def test_single_eval_api_rejects_active_or_non_operation_task(monkeypatch):
    monkeypatch.setattr(server, "cfg", _config)

    active = Task(
        id="api_dataset_001",
        mode="operation",
        items=[],
        options={},
        status="running",
    )
    monkeypatch.setattr(server, "get_task", lambda task_id: active)
    with pytest.raises(HTTPException) as active_error:
        await server.api_eval_single(SingleEvalReq(
            task_id=active.id,
            item=_item(),
        ))
    assert active_error.value.status_code == 409

    other_mode = Task(
        id="api_dataset_001",
        mode="single",
        items=[],
        options={},
        status="done",
    )
    monkeypatch.setattr(server, "get_task", lambda task_id: other_mode)
    with pytest.raises(HTTPException) as mode_error:
        await server.api_eval_single(SingleEvalReq(
            task_id=other_mode.id,
            item=_item(),
        ))
    assert mode_error.value.status_code == 409


@pytest.mark.asyncio
async def test_single_eval_api_validates_task_and_item_ids(monkeypatch):
    monkeypatch.setattr(server, "cfg", _config)

    with pytest.raises(HTTPException) as task_error:
        await server.api_eval_single(SingleEvalReq(
            task_id="bad/task",
            item=_item(),
        ))
    assert task_error.value.status_code == 422

    invalid_item = _item()
    invalid_item.pop("id")
    with pytest.raises(HTTPException) as item_error:
        await server.api_eval_single(SingleEvalReq(
            task_id="api_dataset_001",
            item=invalid_item,
        ))
    assert item_error.value.status_code == 422


@pytest.mark.asyncio
async def test_single_api_runner_executes_different_items_concurrently(monkeypatch):
    task = Task(
        id="api_dataset_001",
        mode="operation",
        items=[
            {"id": "simple_001", "query": "第一题"},
            {"id": "simple_002", "query": "第二题"},
        ],
        options={"concurrency": 4},
        status="running",
    )
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_run(current, app_cfg, *, item_indices=None, rerun=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await release.wait()
        index = item_indices[0]
        runner._upsert_result(current, {
            "index": index,
            "item_id": current.items[index]["id"],
            "correctness": "ok",
        })
        active -= 1

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_prepare_rerun_items", lambda *args: None)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        runner,
        "_summarize",
        lambda current, app_cfg: {"total": len(current.results)},
    )

    first = asyncio.create_task(
        runner.run_single_api_item(task, SimpleNamespace(), 0, "simple_001"),
    )
    second = asyncio.create_task(
        runner.run_single_api_item(task, SimpleNamespace(), 1, "simple_002"),
    )
    task.item_executions = {
        "simple_001": first,
        "simple_002": second,
    }

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)

    assert max_active == 2
    assert task.status == "done"
    assert task.done_total == 2
    assert [event["event"] for event in task.event_log].count("done") == 1


@pytest.mark.asyncio
async def test_single_api_runner_keeps_existing_rerun_audit_shape(monkeypatch):
    task = Task(
        id="api_dataset_001",
        mode="operation",
        items=[{"id": "simple_001", "query": "重跑题"}],
        options={"concurrency": 4},
        status="running",
    )
    attempt = {
        "attempt_id": "rerun-1-abcd1234",
        "attempt_no": 1,
        "item_indices": [0],
        "total": 1,
        "done": 0,
        "status": "running",
        "base_status": "done",
        "started_at": 100.0,
        "items": [],
        "_previous_result": {
            "index": 0,
            "item_id": "simple_001",
            "correctness": "nok",
        },
    }
    task.single_api_attempts["simple_001"] = attempt

    async def fake_run(current, app_cfg, *, item_indices=None, rerun=None):
        runner._upsert_result(current, {
            "index": 0,
            "item_id": "simple_001",
            "correctness": "ok",
            "rerun_count": 1,
        })
        rerun["done"] = 1
        rerun["items"].append({
            "index": 0,
            "item_id": "simple_001",
            "status": "done",
            "previous_status": "done",
        })

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_prepare_rerun_items", lambda *args: None)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner, "_summarize", lambda *args: {"total": 1})

    execution = asyncio.create_task(
        runner.run_single_api_item(
            task,
            SimpleNamespace(),
            0,
            "simple_001",
            rerun=attempt,
        ),
    )
    task.item_executions["simple_001"] = execution
    await execution

    assert task.rerun_history[0]["attempt_id"] == "rerun-1-abcd1234"
    assert task.rerun_history[0]["items"][0]["previous_status"] == "done"
    assert "_previous_result" not in task.rerun_history[0]
    assert task.results[0]["correctness"] == "ok"
