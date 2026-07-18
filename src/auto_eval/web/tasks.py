"""任务管理：内存存储 + asyncio.Queue 作 SSE 事件总线。"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .history import load_snapshot, make_session_name, save_task


@dataclass
class Task:
    id: str
    mode: str
    items: list[dict]
    options: dict
    session_name: str = ""
    status: str = "pending"  # pending | running | done | error
    results: list[dict] = field(default_factory=list)
    item_progress: dict[str, dict] = field(default_factory=dict)
    progress_events: dict[str, list[dict]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    created_at: float = field(default_factory=time.time)
    done_total: int = 0
    error: str | None = None

    async def publish(self, event: str, data: dict) -> None:
        await self.queue.put({"event": event, "data": data})


TASKS: dict[str, Task] = {}


def new_task(mode: str, items: list[dict], options: dict) -> Task:
    task_id = uuid.uuid4().hex[:12]
    created_at = time.time()
    t = Task(
        id=task_id,
        mode=mode,
        items=items,
        options=options,
        session_name=make_session_name(created_at, mode, task_id),
        created_at=created_at,
    )
    TASKS[task_id] = t
    save_task(t)
    return t


def get_task(task_id: str) -> Task | None:
    task = TASKS.get(task_id)
    if task:
        return task
    snapshot = load_snapshot(task_id)
    if not snapshot:
        return None
    status = snapshot.get("status") or "done"
    error = snapshot.get("error")
    if status in {"pending", "running"}:
        status = "error"
        error = error or "服务中断，已保留中断前完成的评估结果"
    task = Task(
        id=snapshot.get("task_id") or task_id,
        mode=snapshot.get("mode") or "single",
        items=snapshot.get("items") or [],
        options=snapshot.get("options") or {},
        session_name=snapshot.get("session_name") or "",
        status=status,
        results=snapshot.get("results") or [],
        item_progress=snapshot.get("item_progress") or {},
        progress_events=snapshot.get("progress_events") or {},
        summary=snapshot.get("summary") or {},
        created_at=float(snapshot.get("created_at") or time.time()),
        done_total=int(snapshot.get("done_total") or len(snapshot.get("results") or [])),
        error=error,
    )
    TASKS[task.id] = task
    return task
