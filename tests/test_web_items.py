"""增量更新接口：请求内分组、按 id 覆写、session 后缀重跑与任务排队。"""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from auto_eval.config import load_config
from auto_eval.web import runner, scheduler, server
from auto_eval.web import tasks as web_tasks
from auto_eval.web.scheduler import EVAL_SCHEDULER, ResizableLimiter
from auto_eval.web.tasks import TASKS, Task, merge_items_by_id

from test_web_scheduler import _patch_runner

_CFG = load_config(Path("config"))


def _patch_item_runner(monkeypatch, expected_calls: int = 20):
    calls: list[tuple[int, str]] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append((idx, item.get("query") or ""))
        return {
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"summary-{item.get('id')}",
        }

    def fake_prepare(item, **kw):
        return {
            **item,
            "video_path": item.get("video_path") or "/tmp/fake.mp4",
            "video_name": "fake.mp4",
            "media": ["/tmp/fake.mp4"],
            "frames": ["/tmp/fake/kf_001.jpg"],
            "frame_count": 1,
            "duration": 1.0,
        }

    _patch_runner(monkeypatch, ResizableLimiter(expected_calls), fake_eval_one)
    monkeypatch.setattr(runner, "prepare_session_rich_content_item", fake_prepare)
    return calls


def _no_persist(monkeypatch):
    monkeypatch.setattr(scheduler, "save_task", lambda task: None)
    monkeypatch.setattr(server, "save_task", lambda task: None)
    monkeypatch.setattr(server, "retire_task", lambda task: None)
    monkeypatch.setattr(web_tasks, "save_task", lambda task: None)


async def _wait_queue_idle(timeout: float = 5.0) -> None:
    async def _idle() -> None:
        while True:
            snap = EVAL_SCHEDULER.snapshot()
            if not snap["running"] and not snap["queued"]:
                return
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_idle(), timeout)


def _task(items: list[dict], *, status: str = "done") -> Task:
    return Task(
        id="items-task",
        mode="rich_content",
        items=items,
        options={},
        status=status,
    )


def test_no_markers_are_independent_single_turn_sessions():
    task = _task([])
    batch, replaced, added = merge_items_by_id(
        task,
        [
            {"id": "q1", "query": "天气"},
            {"id": "q2", "query": "耳机"},
        ],
    )

    assert [idx for idx, _ in batch] == [0, 1]
    assert replaced == []
    assert added == ["q1", "q2"]
    for item in task.items:
        assert "session_group" not in item
        assert item["turn_index"] == 0


def test_start_end_markers_form_one_session():
    task = _task([])
    merge_items_by_id(
        task,
        [
            {"id": "q1", "query": "第一轮", "is_start": True},
            {"id": "q2", "query": "第二轮", "is_end": True},
        ],
    )

    assert task.items[0]["session_group"] == task.items[1]["session_group"]
    assert [item["turn_index"] for item in task.items] == [0, 1]


def test_existing_turn_update_reruns_session_suffix():
    task = _task([
        {"id": "q1", "query": "第一轮", "session_group": "s1", "turn_index": 0},
        {"id": "q2", "query": "第二轮", "session_group": "s1", "turn_index": 1},
        {"id": "q3", "query": "第三轮", "session_group": "s1", "turn_index": 2},
    ])
    task.results = [
        {"index": 0, "turn_summary": "old-1"},
        {"index": 1, "turn_summary": "old-2"},
        {"index": 2, "turn_summary": "old-3"},
    ]

    batch, replaced, added = merge_items_by_id(
        task,
        [{"id": "q2", "query": "新的第二轮"}],
    )
    assert [idx for idx, _ in batch] == [1]
    assert replaced == ["q2"]
    assert added == []
    assert task.items[1]["session_group"] == "s1"
    assert task.items[1]["turn_index"] == 1

    rerun = runner.build_rerun_batches(task, [idx for idx, _ in batch])
    assert len(rerun) == 1
    assert [idx for idx, _ in rerun[0].batch] == [1, 2]
    assert rerun[0].batch[0][1]["query"] == "新的第二轮"
    assert rerun[0].initial_summary == "【第1轮】old-1\n"


def test_multiple_updated_turns_share_one_new_suffix_batch():
    task = _task([
        {"id": "q1", "query": "第一轮", "session_group": "s1", "turn_index": 0},
        {"id": "q2", "query": "第二轮", "session_group": "s1", "turn_index": 1},
        {"id": "q3", "query": "第三轮", "session_group": "s1", "turn_index": 2},
        {"id": "q4", "query": "第四轮", "session_group": "s1", "turn_index": 3},
    ])
    task.results = [
        {"index": 0, "turn_summary": "old-1"},
        {"index": 1, "turn_summary": "old-2"},
        {"index": 2, "turn_summary": "old-3"},
        {"index": 3, "turn_summary": "old-4"},
    ]

    batch, replaced, _ = merge_items_by_id(
        task,
        [
            {"id": "q2", "query": "新的第二轮", "is_start": True},
            {"id": "q3", "query": "新的第三轮", "is_end": True},
        ],
    )
    assert [idx for idx, _ in batch] == [1, 2]
    assert replaced == ["q2", "q3"]

    rerun = runner.build_rerun_batches(task, [idx for idx, _ in batch])
    assert len(rerun) == 1
    assert [idx for idx, _ in rerun[0].batch] == [1, 2, 3]
    assert [item["query"] for _, item in rerun[0].batch] == [
        "新的第二轮",
        "新的第三轮",
        "第四轮",
    ]


def test_unmarked_turns_from_same_existing_session_still_dedupe_rerun():
    task = _task([
        {"id": "q1", "query": "第一轮", "session_group": "s1", "turn_index": 0},
        {"id": "q2", "query": "第二轮", "session_group": "s1", "turn_index": 1},
        {"id": "q3", "query": "第三轮", "session_group": "s1", "turn_index": 2},
    ])
    batch, _, _ = merge_items_by_id(
        task,
        [
            {"id": "q2", "query": "新的第二轮"},
            {"id": "q3", "query": "新的第三轮"},
        ],
    )

    rerun = runner.build_rerun_batches(task, [idx for idx, _ in batch])
    assert len(rerun) == 1
    assert [idx for idx, _ in rerun[0].batch] == [1, 2]


async def test_items_endpoint_queues_multiple_tasks(monkeypatch):
    _no_persist(monkeypatch)
    _patch_item_runner(monkeypatch)
    old_cfg = server._state.get("cfg")
    server._state["cfg"] = _CFG
    response_a = response_b = None
    try:
        response_a = await server.api_eval_items(server.EvalItemsReq(
            task_id="items-queue-a",
            mode="rich_content",
            items=[{"id": "a1", "query": "A", "video_path": "a.mp4"}],
        ))
        response_b = await server.api_eval_items(server.EvalItemsReq(
            task_id="items-queue-b",
            mode="rich_content",
            items=[{"id": "b1", "query": "B", "video_path": "b.mp4"}],
        ))

        assert response_a["status"] == "queued"
        assert response_b["status"] == "queued"
        assert response_a["query_ids"] == ["a1"]
        assert response_b["query_ids"] == ["b1"]
        assert response_a["queue_position"] == 1
        await asyncio.wait_for(
            _wait_until_running("items-queue-a"),
            2,
        )
        snap = EVAL_SCHEDULER.snapshot()
        assert snap["running"]["task_id"] == "items-queue-a"
        assert [job["task_id"] for job in snap["queued"]] == ["items-queue-b"]

        await _wait_queue_idle()
        assert TASKS["items-queue-a"].status == "done"
        assert TASKS["items-queue-b"].status == "done"
    finally:
        await EVAL_SCHEDULER.stop()
        server._state["cfg"] = old_cfg
        for task_id in ("items-queue-a", "items-queue-b"):
            TASKS.pop(task_id, None)


async def test_items_endpoint_runs_updated_session_suffix_once(monkeypatch):
    _no_persist(monkeypatch)
    calls = _patch_item_runner(monkeypatch)
    task = _task([
        {"id": "q1", "query": "第一轮", "session_group": "s1", "turn_index": 0},
        {"id": "q2", "query": "第二轮", "session_group": "s1", "turn_index": 1},
        {"id": "q3", "query": "第三轮", "session_group": "s1", "turn_index": 2},
        {"id": "q4", "query": "第四轮", "session_group": "s1", "turn_index": 3},
    ])
    task.results = [
        {"index": 0, "turn_summary": "old-1"},
        {"index": 1, "turn_summary": "old-2"},
        {"index": 2, "turn_summary": "old-3"},
        {"index": 3, "turn_summary": "old-4"},
    ]
    TASKS[task.id] = task
    old_cfg = server._state.get("cfg")
    server._state["cfg"] = _CFG
    try:
        response = await server.api_eval_items(server.EvalItemsReq(
            task_id=task.id,
            items=[
                {"id": "q2", "query": "新的第二轮", "is_start": True},
                {"id": "q3", "query": "新的第三轮", "is_end": True},
            ],
        ))
        assert response["rerun_ids"] == ["q2", "q3", "q4"]

        await _wait_queue_idle()
        assert [idx for idx, _ in calls] == [1, 2, 3]
        assert [query for _, query in calls] == [
            "新的第二轮",
            "新的第三轮",
            "第四轮",
        ]
    finally:
        await EVAL_SCHEDULER.stop()
        server._state["cfg"] = old_cfg
        TASKS.pop(task.id, None)


async def test_items_endpoint_rejects_same_task_while_queued(monkeypatch):
    _no_persist(monkeypatch)
    _patch_item_runner(monkeypatch)
    old_cfg = server._state.get("cfg")
    server._state["cfg"] = _CFG
    try:
        await server.api_eval_items(server.EvalItemsReq(
            task_id="items-same-task",
            mode="rich_content",
            items=[{"id": "a1", "query": "A", "video_path": "a.mp4"}],
        ))
        with pytest.raises(HTTPException) as exc:
            await server.api_eval_items(server.EvalItemsReq(
                task_id="items-same-task",
                items=[{"id": "a2", "query": "A2", "video_path": "a2.mp4"}],
            ))
        assert exc.value.status_code == 409
    finally:
        await EVAL_SCHEDULER.stop()
        server._state["cfg"] = old_cfg
        TASKS.pop("items-same-task", None)


async def _wait_until_running(task_id: str) -> None:
    while True:
        snap = EVAL_SCHEDULER.snapshot()
        running = snap["running"]
        if running and running["task_id"] == task_id:
            return
        await asyncio.sleep(0.005)
