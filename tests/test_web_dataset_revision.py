import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_eval.web import history, server, tasks
from auto_eval.web.dataset_revision import active_total
from auto_eval.web.server import (
    DatasetBatchRollbackReq,
    DatasetItemsActionReq,
    EvalReq,
)
from auto_eval.web.tasks import TASKS, Task


@pytest.fixture(autouse=True)
def clear_tasks():
    TASKS.clear()
    yield
    for task in TASKS.values():
        if task.execution is not None and not task.execution.done():
            task.execution.cancel()
    TASKS.clear()


def _config():
    return SimpleNamespace(
        judges=[SimpleNamespace(name="judge_2", display="终端用户", persona="end_user")],
    )


def _completed_task() -> Task:
    return Task(
        id="revision-task",
        mode="operation",
        dataset_name="完整任务集.jsonl",
        items=[{
            "id": "generated_001",
            "query": "打开设置",
            "source_data": {"index": "simple_001", "query": "打开设置"},
        }],
        options={"judges": ["judge_2"], "concurrency": 8},
        status="done",
        results=[{
            "index": 0,
            "item_id": "generated_001",
            "query": "打开设置",
            "correctness": "ok",
            "total": 5,
        }],
        done_total=1,
    )


def _fake_summary(task, _cfg):
    task.summary = {
        "total": active_total(task.items),
        "done": task.done_total,
        "failed": 0,
    }
    return task.summary


def test_new_operation_task_archives_initial_normalized_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)

    task = tasks.new_task(
        "operation",
        [{"id": "op_1", "query": "打开设置"}],
        {"judges": ["judge_2"]},
        dataset_name="初始数据.xlsx",
    )

    assert task.items[0]["dataset_status"] == "active"
    assert task.items[0]["dataset_revision"] == 1
    assert len(task.dataset_batches) == 1
    batch = task.dataset_batches[0]
    assert batch["kind"] == "initial"
    path = history.dataset_artifact_path(batch["snapshot_path"])
    assert path is not None and path.is_file()
    rows = history.load_dataset_artifact(batch["snapshot_path"])
    assert isinstance(rows, list)
    assert rows[0]["id"] == "op_1"


def test_exclude_restore_updates_effective_export_and_keeps_result(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "refresh_task_summary", _fake_summary)
    monkeypatch.setattr(server, "save_task", lambda _task: True)

    excluded = server.api_dataset_items_exclude(
        task.id,
        DatasetItemsActionReq(item_indices=[0], reason="误传"),
    )

    assert excluded["active_count"] == 0
    assert excluded["excluded_count"] == 1
    assert task.results[0]["correctness"] == "ok"
    snapshot = history.task_to_snapshot(task)
    assert history.jsonl_export_rows(snapshot) == []
    assert history.export_rows(snapshot)["数据集明细"] == []

    restored = server.api_dataset_items_restore(
        task.id,
        DatasetItemsActionReq(item_indices=[0], reason="确认恢复"),
    )

    assert restored["active_count"] == 1
    assert history.jsonl_export_rows(history.task_to_snapshot(task))[0]["id"] == "generated_001"
    assert [row["action"] for row in task.dataset_change_log[-2:]] == [
        "exclude",
        "restore",
    ]


@pytest.mark.asyncio
async def test_append_replacement_and_insert_can_rollback_with_old_result(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = _completed_task()
    TASKS[task.id] = task

    async def fake_run_append(current, _cfg, _indices):
        attempt = dict(current.active_append or {})
        attempt["status"] = "done"
        current.append_history.append(attempt)
        current.active_append = None
        current.status = "done"

    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(
        server,
        "_normalize_eval_options",
        lambda app_cfg, options: (dict(options), app_cfg),
    )
    monkeypatch.setattr(server, "run_append", fake_run_append)
    monkeypatch.setattr(server, "refresh_task_summary", _fake_summary)

    response = await server.api_eval(EvalReq(
        mode="operation",
        submit_mode="append",
        task_id=task.id,
        dataset_name="修正与补充.xlsx",
        conflict_policy="resolve",
        conflict_resolutions={"simple_001": "replace_and_rerun"},
        items=[
            {
                "id": "incoming_001",
                "query": "打开系统设置",
                "source_data": {"index": "simple_001", "query": "打开系统设置"},
            },
            {
                "id": "incoming_002",
                "query": "关闭蓝牙",
                "source_data": {"index": "simple_002", "query": "关闭蓝牙"},
            },
        ],
        options={"concurrency": 8},
    ))
    execution = task.execution
    assert execution is not None
    await execution
    await asyncio.sleep(0)

    assert response["dataset_size"] == 2
    assert task.items[0]["id"] == "incoming_001"
    assert task.items[0]["dataset_revision"] == 2
    assert task.items[1]["dataset_status"] == "active"
    assert task.results == []
    batch = task.dataset_batches[-1]
    assert history.dataset_artifact_path(batch["snapshot_path"]) is not None
    assert history.dataset_artifact_path(batch["rollback_path"]) is not None

    rolled_back = server.api_dataset_batch_rollback(
        task.id,
        batch["batch_id"],
        DatasetBatchRollbackReq(reason="追加文件有误"),
    )

    assert rolled_back["active_count"] == 1
    assert rolled_back["excluded_count"] == 1
    assert task.items[0]["id"] == "generated_001"
    assert task.items[0]["dataset_revision"] == 1
    assert task.items[1]["dataset_status"] == "excluded"
    assert task.results[0]["correctness"] == "ok"
    assert task.dataset_batches[-1]["status"] == "rolled_back"
    exported = history.jsonl_export_rows(history.task_to_snapshot(task))
    assert [row["id"] for row in exported] == ["generated_001"]


def test_exports_include_dataset_batch_and_change_audit(monkeypatch):
    task = _completed_task()
    TASKS[task.id] = task
    monkeypatch.setattr(server, "cfg", _config)
    monkeypatch.setattr(server, "refresh_task_summary", _fake_summary)
    monkeypatch.setattr(server, "save_task", lambda _task: True)
    server.api_dataset_items_exclude(
        task.id,
        DatasetItemsActionReq(item_indices=[0], reason="冗余数据"),
    )

    sheets = history.export_rows(history.task_to_snapshot(task))

    assert sheets["导入批次"][0]["类型"] == "初始导入"
    assert sheets["数据变更记录"][-1]["动作"] == "排除"
    assert sheets["数据变更记录"][-1]["原因"] == "冗余数据"


def test_frontend_exposes_dataset_maintenance_actions():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")

    assert "数据集维护" in html
    assert "排除选中项" in html
    assert "恢复选中项" in html
    assert "回滚" in html
    assert "dataset/items/${action}" in js
    assert "rollbackDatasetBatch" in js
