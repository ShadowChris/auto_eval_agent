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


def test_history_list_limit_zero_returns_all_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    for index in range(3):
        task = Task(
            id=f"task-{index}",
            mode="operation",
            items=[],
            options={},
            created_at=float(index + 1),
        )
        assert history.save_task(task)

    assert len(history.list_snapshots(limit=2)) == 2
    assert len(history.list_snapshots(limit=0)) == 3


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
        server.HistoryNoteReq(note="  第二轮回归\n已通过  "),
    )

    assert response["note"] == "第二轮回归\n已通过"
    assert task.note == "第二轮回归\n已通过"
    assert saved == ["第二轮回归\n已通过"]


def test_history_note_ui_supports_multiline_editing_and_full_display():
    static_dir = server.STATIC_DIR
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    css = (static_dir / "style.css").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert '<textarea\n                  v-model="historyNoteDrafts[h.task_id]"' in html
    assert '@keydown.ctrl.enter.prevent="saveHistoryNote(h)"' in html
    assert '@keydown.meta.enter.prevent="saveHistoryNote(h)"' in html
    assert '@keyup.enter="saveHistoryNote(h)"' not in html
    assert ".history-note-text {" in css
    assert "white-space: pre-wrap;" in css
    assert "overflow-wrap: anywhere;" in css
    assert ".history-note-editor textarea {" in css
    assert 'v-for="h in pagedHistoryItems"' in html
    assert 'v-model.number="historyPageSize"' in html
    assert "changeHistoryPage(-1)" in html
    assert "jumpTablePage('history')" in html
    assert 'fetch("/api/history?limit=0")' in js
    assert 'v-for="h in pagedHistoryItems"' in html
    assert 'cancelHistoryTask(h)' in html
    assert "historyStatusLabel(h.status)" in html
    assert "function resetEvaluationView()" in js
    assert "resetEvaluationView();\n      mode.value = k;" in js
    assert "if (running.value) connectSSE();" in js
    assert "new EventSource(`/api/eval/${connectedTaskId}/stream`)" in js
    assert 'es.addEventListener("task_state"' in js
    assert "total.value = Number.isFinite(Number(d.total))" in js
    assert "progress.value = Number.isFinite(Number(d.done_total))" in js


def test_history_note_api_limits_length(monkeypatch):
    task = Task(id="note-task", mode="operation", items=[], options={})
    monkeypatch.setattr(server, "get_task", lambda _: task)

    with pytest.raises(HTTPException, match="1000"):
        server.api_history_note(
            task.id,
            server.HistoryNoteReq(note="x" * 1001),
        )
