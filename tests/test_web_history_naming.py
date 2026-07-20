import json
from datetime import datetime

from auto_eval.web import history
from auto_eval.web.tasks import Task


def test_new_history_name_is_time_sortable_and_loadable_by_task_id(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    created_at = datetime(2026, 7, 17, 10, 39, 30).astimezone().timestamp()
    session_name = history.make_session_name(created_at, "operation", "aa5cd32001ec")
    task = Task(
        id="aa5cd32001ec",
        mode="operation",
        items=[],
        options={},
        session_name=session_name,
        created_at=created_at,
    )

    assert history.save_task(task)
    path = tmp_path / "20260717_103930_operation_aa5cd32001ec.json"
    assert path.exists()
    assert history.load_snapshot(task.id)["session_name"] == session_name
    assert history.list_snapshots()[0]["session_name"] == session_name
    assert history.delete_snapshot(task.id)
    assert not path.exists()


def test_legacy_history_filename_remains_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    legacy = tmp_path / "legacy123.json"
    legacy.write_text(json.dumps({
        "task_id": "legacy123",
        "mode": "single",
        "items": [],
        "results": [],
        "status": "done",
        "created_at": 1_700_000_000,
    }), encoding="utf-8")

    assert history.load_snapshot("legacy123")["task_id"] == "legacy123"
    assert history.delete_snapshot("legacy123")
    assert not legacy.exists()
