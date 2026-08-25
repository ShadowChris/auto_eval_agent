from pathlib import Path

import pytest

from auto_eval.web import history, server, tasks
from auto_eval.web.tasks import TASKS, Task


@pytest.fixture(autouse=True)
def clear_task_cache():
    TASKS.clear()
    yield
    TASKS.clear()


def _completed(task_id: str, accessed_at: float) -> Task:
    task = Task(
        id=task_id,
        mode="operation",
        items=[{"id": task_id, "query": "打开设置"}],
        options={},
        status="done",
    )
    task.last_accessed_at = accessed_at
    return task


def test_prune_task_cache_keeps_active_and_recent_completed(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_EVAL_MAX_CACHED_COMPLETED_TASKS", raising=False)
    monkeypatch.delenv("AUTO_EVAL_COMPLETED_TASK_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(tasks, "MAX_CACHED_COMPLETED_TASKS", 2)
    monkeypatch.setattr(tasks, "COMPLETED_TASK_CACHE_TTL_S", 100.0)
    TASKS.update({
        "old": _completed("old", 10.0),
        "recent-1": _completed("recent-1", 20.0),
        "recent-2": _completed("recent-2", 30.0),
        "running": Task(
            id="running",
            mode="operation",
            items=[],
            options={},
            status="running",
        ),
    })

    removed = tasks.prune_task_cache(now=31.0)

    assert removed == ["old"]
    assert set(TASKS) == {"recent-1", "recent-2", "running"}


def test_prune_task_cache_expires_completed_tasks(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_EVAL_MAX_CACHED_COMPLETED_TASKS", raising=False)
    monkeypatch.delenv("AUTO_EVAL_COMPLETED_TASK_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(tasks, "MAX_CACHED_COMPLETED_TASKS", 8)
    monkeypatch.setattr(tasks, "COMPLETED_TASK_CACHE_TTL_S", 10.0)
    TASKS["expired"] = _completed("expired", 1.0)

    assert tasks.prune_task_cache(now=20.0) == ["expired"]
    assert not TASKS


def test_prune_task_cache_keeps_completed_task_with_live_subscriber(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTO_EVAL_MAX_CACHED_COMPLETED_TASKS", raising=False)
    monkeypatch.delenv("AUTO_EVAL_COMPLETED_TASK_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(tasks, "MAX_CACHED_COMPLETED_TASKS", 0)
    task = _completed("connected", 1.0)
    task.subscribe()
    TASKS[task.id] = task

    assert tasks.prune_task_cache(now=100.0) == []
    assert task.id in TASKS


def test_terminal_event_log_and_sse_queue_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "MAX_TERMINAL_EVENT_LOG_SIZE", 3)
    monkeypatch.setattr(tasks, "MAX_SSE_QUEUE_SIZE", 2)
    task = _completed("bounded", 1.0)
    queue = task.subscribe()

    for index in range(5):
        task.publish_nowait("item_progress", {"index": index})
    task.publish_nowait("done", {"ok": True})

    assert len(task.event_log) == 3
    assert queue.qsize() == 2
    queued = [queue.get_nowait(), queue.get_nowait()]
    assert [message["cursor"] for message in queued] == [5, 6]
    assert queued[-1]["event"] == "done"


def test_history_detail_reads_completed_snapshot_without_caching(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = _completed("history-only", 1.0)
    task.dataset_name = "history.jsonl"
    assert history.save_task(task)

    payload = server.api_history_detail(task.id, compact=True)

    assert payload["task_id"] == task.id
    assert payload["dataset_name"] == "history.jsonl"
    assert task.id not in TASKS


def test_delete_history_also_releases_completed_memory_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = _completed("delete-me", 1.0)
    TASKS[task.id] = task
    assert history.save_task(task)

    assert server.api_history_delete(task.id) == {"ok": True}
    assert task.id not in TASKS
    assert history.load_snapshot(task.id) is None


def test_exporting_old_history_does_not_add_it_to_memory_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = _completed("export-only", 1.0)
    assert history.save_task(task)

    response = server.api_export(task.id, format="json")

    assert response.status_code == 200
    assert task.id not in TASKS
