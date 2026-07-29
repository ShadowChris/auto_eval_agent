import json
from datetime import datetime

import pytest
from fastapi import HTTPException

from auto_eval.web import history
from auto_eval.web import server
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
        dataset_name="operation_cases.jsonl",
        note="首轮回归，留档",
        session_name=session_name,
        created_at=created_at,
    )

    assert history.save_task(task)
    path = tmp_path / "20260717_103930_operation_aa5cd32001ec.json"
    assert path.exists()
    assert history.load_snapshot(task.id)["session_name"] == session_name
    assert history.list_snapshots()[0]["session_name"] == session_name
    assert history.list_snapshots()[0]["dataset_name"] == "operation_cases.jsonl"
    assert history.list_snapshots()[0]["note"] == "首轮回归，留档"
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


def test_history_note_api_updates_task_and_persists(monkeypatch):
    task = Task(
        id="note-task",
        mode="operation",
        items=[],
        options={},
        note="旧备注",
    )
    saved = []
    monkeypatch.setattr(server, "get_task", lambda _: task)
    monkeypatch.setattr(server, "save_task", lambda value: saved.append(value.note) or True)

    response = server.api_history_note(
        task.id,
        server.HistoryNoteReq(note="  第二轮回归通过  "),
    )

    assert response["note"] == "第二轮回归通过"
    assert task.note == "第二轮回归通过"
    assert saved == ["第二轮回归通过"]


def test_history_note_api_limits_length(monkeypatch):
    task = Task(id="note-task", mode="operation", items=[], options={})
    monkeypatch.setattr(server, "get_task", lambda _: task)

    with pytest.raises(HTTPException, match="1000"):
        server.api_history_note(
            task.id,
            server.HistoryNoteReq(note="x" * 1001),
        )
