import asyncio
from types import SimpleNamespace

import pytest

from fastapi import HTTPException

from auto_eval.web import history, runner, server
from auto_eval.web.server import RerunReq
from auto_eval.web.tasks import TASKS, Task, get_task


def _task() -> Task:
    return Task(
        id="rerun-task",
        mode="operation",
        items=[
            {"id": "simple_001", "query": "打开设置"},
            {"id": "simple_002", "query": "关闭蓝牙"},
        ],
        options={"judges": ["end_user"], "concurrency": 8},
        status="done",
        results=[
            {"index": 0, "item_id": "simple_001", "correctness": "ok"},
            {"index": 1, "item_id": "simple_002", "error": "timeout"},
        ],
        done_total=2,
        started_at=100.0,
        finished_at=120.0,
        duration_s=20.0,
    )


def test_result_upsert_replaces_original_index_without_duplicate():
    task = _task()

    previous = runner._upsert_result(
        task,
        {
            "index": 1,
            "item_id": "simple_002",
            "correctness": "ok",
            "rerun_count": 1,
        },
    )

    assert previous == {"index": 1, "item_id": "simple_002", "error": "timeout"}
    assert [row["index"] for row in task.results] == [0, 1]
    assert len(task.results) == 2
    assert task.results[1]["correctness"] == "ok"
    assert task.done_total == 2


@pytest.mark.asyncio
async def test_rerun_keeps_original_timing_and_records_audit(monkeypatch):
    task = _task()

    async def fake_run(current, cfg, *, item_indices=None, rerun=None):
        assert item_indices == [1]
        result = {
            "index": 1,
            "item_id": "simple_002",
            "correctness": "ok",
            "rerun_count": 1,
        }
        previous = runner._upsert_result(current, result)
        rerun["done"] = 1
        rerun["items"].append({
            "index": 1,
            "item_id": "simple_002",
            "status": "done",
            "previous_status": "error" if previous.get("error") else "done",
        })

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_prepare_rerun_items", lambda *args: None)
    monkeypatch.setattr(runner, "_summarize", lambda current, cfg: {"total": 2, "failed": 0})
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)

    await runner.run_rerun(task, SimpleNamespace(), [1])

    assert task.status == "done"
    assert task.started_at == 100.0
    assert task.finished_at == 120.0
    assert task.duration_s == 20.0
    assert task.active_rerun is None
    assert task.results[1]["correctness"] == "ok"
    assert len(task.rerun_history) == 1
    assert task.rerun_history[0]["item_indices"] == [1]
    assert task.rerun_history[0]["items"][0]["previous_status"] == "error"
    assert task.event_log[-1]["event"] == "rerun_done"


def test_missing_cached_frames_are_cleared_before_rerun(tmp_path):
    existing = tmp_path / "existing.jpg"
    existing.write_bytes(b"frame")
    task = _task()
    task.items[0]["frames"] = [str(existing)]
    task.items[1]["frames"] = [str(tmp_path / "missing.jpg")]
    task.items[1]["frame_count"] = 1

    runner._prepare_rerun_items(task, [0, 1])

    assert task.items[0]["frames"] == [str(existing)]
    assert "frames" not in task.items[1]
    assert "frame_count" not in task.items[1]


def test_interrupted_rerun_recovers_parent_and_preserves_merged_results(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = _task()
    task.status = "rerunning"
    task.active_rerun = {
        "attempt_id": "rerun-1-deadbeef",
        "attempt_no": 1,
        "base_status": "done",
        "item_indices": [1],
        "started_at": 200.0,
        "done": 0,
        "total": 1,
        "items": [],
    }
    history.save_task(task)
    TASKS.pop(task.id, None)

    recovered = get_task(task.id)

    assert recovered is not None
    assert recovered.status == "done"
    assert recovered.active_rerun is None
    assert recovered.rerun_history[-1]["status"] == "interrupted"
    assert len(recovered.results) == 2
    TASKS.pop(task.id, None)


def test_export_contains_latest_rerun_fields_and_audit_sheet(tmp_path):
    task = _task()
    task.results[1] = {
        "index": 1,
        "item_id": "simple_002",
        "query": "关闭蓝牙",
        "correctness": "ok",
        "rerun_count": 1,
        "last_rerun_at": 300.0,
        "judge_provider": "Provider Two",
        "judge_provider_id": "p2",
        "judge_model": "m2",
        "judge_provider_revision": "rev-2",
    }
    task.rerun_history = [{
        "attempt_id": "rerun-1-abcd1234",
        "attempt_no": 1,
        "item_indices": [1],
        "status": "done",
        "started_at": 290.0,
        "finished_at": 300.0,
        "duration_s": 10.0,
        "judge_backend": {
            "provider_id": "p2",
            "provider_name": "Provider Two",
            "model": "m2",
            "provider_revision": "rev-2",
        },
        "items": [{
            "index": 1,
            "item_id": "simple_002",
            "status": "done",
            "previous_status": "error",
            "correctness": "ok",
            "judge_provider": "Provider Two",
            "judge_provider_id": "p2",
            "judge_model": "m2",
            "judge_provider_revision": "rev-2",
        }],
    }]

    sheets = history.export_rows(history.task_to_snapshot(task))

    assert sheets["逐题结果"][1]["重跑次数"] == 1
    assert sheets["逐题结果"][1]["最后重跑时间"]
    assert sheets["逐题结果"][1]["Provider"] == "Provider Two"
    assert sheets["逐题结果"][1]["模型"] == "m2"
    assert sheets["重跑记录"][0]["数据集序号"] == 2
    assert sheets["重跑记录"][0]["重跑前状态"] == "error"
    assert sheets["重跑记录"][0]["Provider"] == "Provider Two"
    assert sheets["重跑记录"][0]["模型"] == "m2"
    assert sheets["运行汇总"][0]["rerun_count"] == 1


def test_frontend_exposes_manual_and_failed_item_rerun_actions():
    html = (history.PROJECT_ROOT / "src/auto_eval/web/static/index.html").read_text(
        encoding="utf-8",
    )
    js = (history.PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(
        encoding="utf-8",
    )

    assert "选择全部失败项" in html
    assert "重跑选中项" in html
    assert "rerunOne(r)" in html
    assert 'filter((result) => Boolean(result?.error) || (' in js
    assert 'group?.evaluation_status === "error"' in js
    assert 'fetch(`/api/eval/${taskId.value}/rerun`' in js
    assert "judge_backend: rerunBackend" in js
    assert "本次使用：${rerunBackendLabel}" in js
    assert "本次重跑（{{ rerunProgressIndices.length }}）" in html
    assert "完整进度（{{ progressRows.length }}）" in html
    assert 'setProgressView("rerun")' in js
    assert "visibleProgressRows" in js


@pytest.mark.asyncio
async def test_rerun_api_validates_indices_and_starts_one_parent_execution(monkeypatch):
    task = _task()
    task.options["judge_backend"] = {
        "provider_id": "old-provider",
        "model": "old-model",
    }
    TASKS[task.id] = task
    started = asyncio.Event()
    release = asyncio.Event()
    runtime_cfg = object()

    def fake_normalize(app_cfg, options):
        assert options["judge_backend"] == {
            "provider_id": "new-provider",
            "model": "new-model",
        }
        normalized = dict(options)
        normalized["judge_backend"] = {
            "provider_id": "new-provider",
            "provider_name": "New Provider",
            "model": "new-model",
            "provider_revision": "rev-new",
        }
        return normalized, runtime_cfg

    async def fake_rerun(current, cfg, indices, *, base_status=None):
        assert indices == [1]
        assert base_status == "done"
        assert cfg is runtime_cfg
        assert current.active_rerun["judge_backend"]["provider_id"] == "new-provider"
        started.set()
        await release.wait()
        current.status = "done"

    monkeypatch.setattr(server, "run_rerun", fake_rerun)
    monkeypatch.setattr(server, "cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(server, "_normalize_eval_options", fake_normalize)
    monkeypatch.setattr(server, "save_task", lambda current: True)
    try:
        response = await server.api_eval_rerun(
            task.id,
            RerunReq(
                item_indices=[1, 1],
                judge_backend={
                    "provider_id": "new-provider",
                    "model": "new-model",
                },
            ),
        )
        assert response["item_indices"] == [1]
        assert response["judge_backend"]["provider_id"] == "new-provider"
        assert task.status == "rerunning"
        await started.wait()

        with pytest.raises(HTTPException) as exc_info:
            await server.api_eval_rerun(task.id, RerunReq(item_indices=[0]))
        assert exc_info.value.status_code == 409

        release.set()
        assert task.execution is not None
        await task.execution
        await asyncio.sleep(0)
        assert task.status == "done"
    finally:
        if task.execution is not None and not task.execution.done():
            task.execution.cancel()
        TASKS.pop(task.id, None)


@pytest.mark.asyncio
async def test_rerun_api_rejects_out_of_range_index():
    task = _task()
    TASKS[task.id] = task
    try:
        with pytest.raises(HTTPException) as exc_info:
            await server.api_eval_rerun(task.id, RerunReq(item_indices=[99]))
        assert exc_info.value.status_code == 422
    finally:
        TASKS.pop(task.id, None)


@pytest.mark.asyncio
async def test_immediate_rerun_cancel_restores_parent_status(monkeypatch):
    task = _task()
    TASKS[task.id] = task
    entered = False

    async def not_yet_started(current, cfg, indices, *, base_status=None):
        nonlocal entered
        entered = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "run_rerun", not_yet_started)
    monkeypatch.setattr(server, "cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(server, "save_task", lambda current: True)
    try:
        await server.api_eval_rerun(task.id, RerunReq(item_indices=[1]))
        response = await server.api_eval_cancel(task.id)

        assert entered is False
        assert response["status"] == "done"
        assert task.status == "done"
        assert task.active_rerun is None
        assert task.rerun_history[-1]["status"] == "cancelled"
    finally:
        TASKS.pop(task.id, None)
