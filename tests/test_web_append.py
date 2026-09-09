import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auto_eval.web import history, runner, server, tasks
from auto_eval.web.server import EvalReq
from auto_eval.web.tasks import TASKS, Task, get_task


@pytest.fixture(autouse=True)
def clear_task_cache():
    TASKS.clear()
    yield
    for task in TASKS.values():
        if task.execution is not None and not task.execution.done():
            task.execution.cancel()
    TASKS.clear()


def _config():
    return SimpleNamespace(
        judges=[
            SimpleNamespace(
                name="judge_2",
                display="终端用户",
                persona="end_user",
            ),
        ],
    )


def _completed_task() -> Task:
    return Task(
        id="append-task",
        mode="operation",
        dataset_name="完整任务集.jsonl",
        items=[{
            "id": "generated_001",
            "query": "打开设置",
            "source_data": {"index": "simple_001", "query": "打开设置"},
        }],
        options={
            "judges": ["judge_2"],
            "concurrency": 4,
            "eval_timeout_s": 300,
            "judge_backend": {
                "provider_id": "target-provider",
                "provider_name": "目标 Provider",
                "model": "target-model",
                "provider_revision": "rev-old",
            },
        },
        status="done",
        results=[{
            "index": 0,
            "item_id": "generated_001",
            "query": "打开设置",
            "correctness": "ok",
        }],
        done_total=1,
        started_at=100.0,
        finished_at=120.0,
        duration_s=20.0,
    )


@pytest.mark.asyncio
async def test_eval_append_reuses_task_and_only_runs_new_indices(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    captured = {}

    def fake_normalize(app_cfg, options):
        captured["options"] = dict(options)
        assert options["judge_backend"]["provider_id"] == "target-provider"
        assert options["judge_backend"]["model"] == "target-model"
        assert options["concurrency"] == 8
        assert options["eval_timeout_s"] == 600
        return dict(options), "runtime-config"

    async def fake_run_append(current, app_cfg, indices):
        captured["indices"] = list(indices)
        captured["runtime_cfg"] = app_cfg

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "_normalize_eval_options", fake_normalize)
    monkeypatch.setattr(server, "run_append", fake_run_append)
    monkeypatch.setattr(server, "save_task", lambda current: True)

    response = await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        dataset_name="第二段.xlsx",
        items=[{
            "id": "generated_002",
            "query": "关闭蓝牙",
            "source_data": {"index": "simple_002", "query": "关闭蓝牙"},
        }],
        options={
            "concurrency": 8,
            "eval_timeout_s": 600,
            "judge_backend": {
                "provider_id": "ignored-provider",
                "model": "ignored-model",
            },
        },
    ))

    execution = task.execution
    assert response["task_id"] == task.id
    assert response["action"] == "appended"
    assert response["item_indices"] == [1]
    assert response["dataset_size"] == 2
    assert task.dataset_name == "完整任务集.jsonl"
    assert task.items[1]["evaluation_segment_no"] == 2
    assert task.items[1]["evaluation_source_dataset"] == "第二段.xlsx"
    assert task.active_append["item_indices"] == [1]
    assert task.finished_at is None
    assert execution is not None
    await execution
    await asyncio.sleep(0)
    assert captured["indices"] == [1]
    assert captured["runtime_cfg"] == "runtime-config"


@pytest.mark.asyncio
async def test_eval_append_rejects_duplicate_without_mutating_task(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )

    with pytest.raises(HTTPException) as exc_info:
        await server.api_eval(EvalReq(
            mode="operation",
            submit_mode="append",
            task_id=task.id,
            dataset_name="重复段.csv",
            items=[{
                "id": "other-id",
                "query": "重复题目",
                "source_data": {"index": "simple_001"},
            }],
            options={"concurrency": 8},
        ))

    assert exc_info.value.status_code == 409
    assert "simple_001" in str(exc_info.value.detail)
    assert len(task.items) == 1
    assert task.active_append is None
    assert task.status == "done"


@pytest.mark.asyncio
async def test_immediate_append_cancel_keeps_new_items_and_closes_attempt(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    entered = False

    async def not_yet_started(current, app_cfg, indices):
        nonlocal entered
        entered = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )
    monkeypatch.setattr(server, "run_append", not_yet_started)
    monkeypatch.setattr(server, "save_task", lambda current: True)

    await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        dataset_name="第二段.xlsx",
        items=[{
            "id": "generated_002",
            "query": "关闭蓝牙",
            "source_data": {"index": "simple_002"},
        }],
        options={"concurrency": 8},
    ))
    response = await server.api_eval_cancel(task.id)

    assert entered is False
    assert response["status"] == "cancelled"
    assert len(task.items) == 2
    assert task.active_append is None
    assert task.append_history[-1]["status"] == "cancelled"
    assert task.item_progress["1"]["status"] == "cancelled"


def test_dataset_item_key_prefers_original_index_and_sequence():
    assert server._dataset_item_key({
        "id": "generated-id",
        "source_data": {"index": 12.0, "序号": "simple_012"},
    }) == "12"
    assert server._dataset_item_key({
        "id": "generated-id",
        "source_data": {"序号": "simple_012"},
    }) == "simple_012"
    assert server._dataset_item_key({"id": "simple_012"}) == "simple_012"


@pytest.mark.asyncio
async def test_append_preview_recommends_keep_replace_and_requires_query_choice(
    monkeypatch,
):
    task = _completed_task()
    task.results[0]["correctness"] = "nok"
    task.items.extend([
        {
            "id": "generated_002",
            "query": "关闭蓝牙",
            "source_data": {"index": "simple_002"},
        },
        {
            "id": "generated_003",
            "query": "打开 WLAN",
            "source_data": {"index": "simple_003"},
        },
    ])
    task.results.append({
        "index": 1,
        "item_id": "generated_002",
        "query": "关闭蓝牙",
        "error": "model timeout",
    })
    task.results.append({
        "index": 2,
        "item_id": "generated_003",
        "query": "打开 WLAN",
        "correctness": "ok",
    })
    task.done_total = 3
    TASKS[task.id] = task
    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )

    response = await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        conflict_policy="preview",
        dataset_name="补充分段.xlsx",
        items=[
            {
                "id": "incoming_001",
                "query": "打开设置",
                "source_data": {"index": "simple_001"},
            },
            {
                "id": "incoming_002",
                "query": "关闭蓝牙",
                "source_data": {"index": "simple_002"},
            },
            {
                "id": "incoming_003",
                "query": "关闭 WLAN",
                "source_data": {"index": "simple_003"},
            },
            {
                "id": "incoming_004",
                "query": "打开相机",
                "context": "测试机已解锁",
                "video_path": "/datasets/videos/simple_004.mp4",
                "query_images": ["/datasets/images/simple_004.png"],
                "task_start_time": 1.5,
                "task_end_time": 8,
                "source_data": {"index": "simple_004"},
            },
        ],
        options={"concurrency": 8},
    ))

    preview = response["merge_preview"]
    assert response["action"] == "preview"
    assert preview["new_count"] == 1
    assert preview["conflict_count"] == 3
    assert preview["recommended_keep_count"] == 1
    assert preview["recommended_replace_count"] == 1
    assert preview["unresolved_count"] == 1
    assert preview["new_items"] == [{
        "incoming_position": 3,
        "incoming_no": 4,
        "key": "simple_004",
        "incoming_id": "incoming_004",
        "query": "打开相机",
        "context": "测试机已解锁",
        "video_path": "/datasets/videos/simple_004.mp4",
        "query_images": ["/datasets/images/simple_004.png"],
        "task_start_time": 1.5,
        "task_end_time": 8,
    }]
    by_key = {row["key"]: row for row in preview["conflicts"]}
    # correctness=nok 是有效评测结果，不属于技术失败。
    assert by_key["simple_001"]["existing_evaluation_status"] == "succeeded"
    assert by_key["simple_001"]["recommended_action"] == "keep_existing"
    assert by_key["simple_002"]["recommended_action"] == "replace_and_rerun"
    assert by_key["simple_003"]["recommended_action"] is None
    assert len(task.items) == 3
    assert task.active_append is None


@pytest.mark.asyncio
async def test_append_resolve_keeps_replaces_and_inserts_in_one_run(monkeypatch):
    task = _completed_task()
    task.items.append({
        "id": "generated_002",
        "query": "关闭蓝牙",
        "source_data": {"index": "simple_002"},
    })
    task.results.append({
        "index": 1,
        "item_id": "generated_002",
        "query": "关闭蓝牙",
        "error": "model timeout",
    })
    task.done_total = 2
    TASKS[task.id] = task
    captured = {}

    async def fake_run_append(current, app_cfg, indices):
        captured["indices"] = list(indices)
        captured["results"] = list(current.results)

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )
    monkeypatch.setattr(server, "run_append", fake_run_append)
    monkeypatch.setattr(server, "save_task", lambda current: True)

    response = await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        conflict_policy="resolve",
        conflict_resolutions={
            "simple_001": "keep_existing",
            "simple_002": "replace_and_rerun",
        },
        dataset_name="补充分段.xlsx",
        items=[
            {
                "id": "incoming_001",
                "query": "打开设置",
                "source_data": {"index": "simple_001"},
            },
            {
                "id": "incoming_002",
                "query": "关闭蓝牙",
                "source_data": {"index": "simple_002"},
            },
            {
                "id": "incoming_003",
                "query": "打开相机",
                "source_data": {"index": "simple_003"},
            },
        ],
        options={"concurrency": 8},
    ))

    execution = task.execution
    assert response["merge_summary"] == {
        "incoming_count": 3,
        "inserted_count": 1,
        "replaced_count": 1,
        "skipped_count": 1,
        "conflict_count": 2,
    }
    assert response["item_indices"] == [1, 2]
    assert task.items[0]["id"] == "generated_001"
    assert task.items[1]["id"] == "incoming_002"
    assert task.items[2]["id"] == "incoming_003"
    assert task.items[1]["evaluation_source_dataset"] == "补充分段.xlsx"
    assert [row["index"] for row in task.results] == [0]
    assert task.active_append["skipped_item_keys"] == ["simple_001"]
    assert execution is not None
    await execution
    assert captured["indices"] == [1, 2]
    assert [row["index"] for row in captured["results"]] == [0]


@pytest.mark.asyncio
async def test_append_all_keep_records_audit_without_starting_runner(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )
    monkeypatch.setattr(
        server,
        "run_append",
        lambda *args, **kwargs: pytest.fail("all-kept merge must not start runner"),
    )
    monkeypatch.setattr(server, "save_task", lambda current: True)

    response = await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        conflict_policy="resolve",
        conflict_resolutions={"simple_001": "keep_existing"},
        dataset_name="重复分段.xlsx",
        items=[{
            "id": "incoming_001",
            "query": "打开设置",
            "source_data": {"index": "simple_001"},
        }],
        options={"concurrency": 8},
    ))

    assert response["action"] == "skipped"
    assert response["item_indices"] == []
    assert len(task.items) == 1
    assert task.status == "done"
    assert task.append_history[-1]["skipped_count"] == 1


@pytest.mark.asyncio
async def test_run_append_merges_result_and_accumulates_duration(monkeypatch):
    task = _completed_task()
    task.items.append({
        "id": "generated_002",
        "query": "关闭蓝牙",
        "evaluation_segment_no": 2,
        "evaluation_source_dataset": "第二段.xlsx",
    })
    task.status = "running"
    task.finished_at = None
    task.active_append = {
        "append_id": "append-test",
        "segment_no": 2,
        "source_dataset_name": "第二段.xlsx",
        "item_indices": [1],
        "total": 1,
        "done": 0,
        "status": "starting",
        "started_at": 200.0,
        "base_duration_s": 20.0,
    }

    async def fake_run(
        current,
        app_cfg,
        *,
        item_indices=None,
        rerun=None,
        append_attempt=None,
        evaluation_timestamp=None,
    ):
        assert item_indices == [1]
        assert rerun is None
        assert evaluation_timestamp == 200.0
        runner._upsert_result(current, {
            "index": 1,
            "item_id": "generated_002",
            "query": "关闭蓝牙",
            "correctness": "ok",
        })
        append_attempt["done"] = 1

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_summarize", lambda current, cfg: {"total": 2})
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner.time, "time", lambda: 205.0)

    await runner.run_append(task, SimpleNamespace(), [1])

    assert task.status == "done"
    assert task.done_total == 2
    assert task.duration_s == 25.0
    assert task.finished_at == 205.0
    assert task.active_append is None
    assert task.append_history[0]["status"] == "done"
    assert task.append_history[0]["duration_s"] == 5.0
    assert task.event_log[-1]["event"] == "done"


def test_interrupted_append_recovers_auditable_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(tasks.time, "time", lambda: 250.0)
    task = _completed_task()
    task.items.append({"id": "generated_002", "query": "关闭蓝牙"})
    task.status = "running"
    task.finished_at = None
    task.item_progress["1"] = {
        "item_index": 1,
        "status": "running",
        "message": "正在评估",
    }
    task.active_append = {
        "append_id": "append-interrupted",
        "segment_no": 2,
        "source_dataset_name": "第二段.xlsx",
        "item_indices": [1],
        "total": 1,
        "done": 0,
        "status": "running",
        "started_at": 200.0,
        "base_duration_s": 20.0,
    }
    assert history.save_task(task)
    TASKS.pop(task.id, None)

    recovered = get_task(task.id)

    assert recovered is not None
    assert recovered.status == "cancelled"
    assert recovered.active_append is None
    assert recovered.append_history[-1]["status"] == "interrupted"
    assert recovered.duration_s == 70.0
    assert recovered.finished_at == 250.0
    assert recovered.item_progress["1"]["status"] == "cancelled"


def test_append_audit_and_segment_fields_are_exported():
    task = _completed_task()
    task.items.append({
        "id": "generated_002",
        "query": "关闭蓝牙",
        "evaluation_segment_no": 2,
        "evaluation_source_dataset": "第二段.xlsx",
    })
    task.results.append({
        "index": 1,
        "item_id": "generated_002",
        "query": "关闭蓝牙",
        "correctness": "ok",
    })
    task.append_history = [{
        "append_id": "append-test",
        "segment_no": 2,
        "source_dataset_name": "第二段.xlsx",
        "item_indices": [1],
        "total": 1,
        "done": 1,
        "status": "done",
        "started_at": 200.0,
        "finished_at": 205.0,
        "duration_s": 5.0,
    }]

    snapshot = history.task_to_snapshot(task)
    sheets = history.export_rows(snapshot)
    jsonl_rows = history.jsonl_export_rows(snapshot)

    assert sheets["数据集明细"][0]["评估分段"] == 1
    assert sheets["数据集明细"][1]["评估分段"] == 2
    assert sheets["逐题结果"][1]["分段来源数据集"] == "第二段.xlsx"
    assert sheets["追加记录"][0]["append_id"] == "append-test"
    assert sheets["运行汇总"][0]["append_count"] == 1
    assert jsonl_rows[1]["eval_run"]["segment_no"] == 2


def test_frontend_exposes_append_target_and_same_task_submission():
    html = (history.PROJECT_ROOT / "src/auto_eval/web/static/index.html").read_text(
        encoding="utf-8",
    )
    js = (history.PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(
        encoding="utf-8",
    )

    assert "追加到历史评估集" in html
    assert "追加数据" in html
    assert "正在自动预检数据冲突" in html
    assert "确认追加并评估" in html
    assert "合并预检" in html
    assert "新增数据预览" in html
    assert "冲突预检" in html
    assert "新增数据（{{ appendMergePreview.new_count || 0 }}）" in html
    assert "冲突数据（{{ appendMergePreview.conflict_count || 0 }}）" in html
    assert "appendPreviewTab==='new'" in html
    assert "appendPreviewTab==='conflicts'" in html
    assert "pagedAppendNewPreviewItems" in js
    assert "setAppendNewPreviewPage" in js
    assert "取消勾选的数据不会追加，也不会参与评估" in html
    assert "setAllAppendNewSelections" in html
    assert "appendNewSelections" in js
    assert "items: submittedItems" in js
    assert 'const opPageSize = 2;' in js
    assert "清空导入数据" in html
    assert "取消勾选的数据不会提交评估" in html
    assert 'v-if="!hasImportedOperationDataset"' in html
    assert "pagedImportedDatasetPreviewRows" in js
    assert "setAllImportedDatasetSelections" in js
    assert "全部保留旧数据" in html
    assert "全部使用新数据" in html
    assert "确认追加并评估" in html
    assert 'submit_mode: isAppendSubmit ? "append" : "create"' in js
    assert "task_id: isAppendSubmit ? appendTargetTaskId.value" in js
    assert '? (isConfirmedAppend ? "resolve" : "preview")' in js
    assert "canAppendHistoryItem" in js
    assert "await submit(false, true)" in js
