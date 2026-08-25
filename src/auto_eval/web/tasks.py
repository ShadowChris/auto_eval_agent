"""任务管理：内存存储 + 每连接独立的 SSE 事件订阅。"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .history import load_snapshot, make_session_name, save_task


def _nonnegative_env_number(name: str, default: str, cast):
    try:
        return max(cast(0), cast(os.getenv(name, default)))
    except (TypeError, ValueError):
        return cast(default)


MAX_EVENT_LOG_SIZE = 20_000
MAX_TERMINAL_EVENT_LOG_SIZE = 200
MAX_SSE_QUEUE_SIZE = 2_000
MAX_CACHED_COMPLETED_TASKS = 8
COMPLETED_TASK_CACHE_TTL_S = 1800.0
_ACTIVE_STATUSES = {"pending", "running", "rerunning"}
_TERMINAL_EVENTS = {"done", "error", "cancelled", "rerun_done", "rerun_cancelled"}


@dataclass
class Task:
    id: str
    mode: str
    items: list[dict]
    options: dict
    session_name: str = ""
    dataset_name: str = ""
    note: str = ""
    judge_trace_path: str = ""
    status: str = "pending"  # pending | running | rerunning | done | error | cancelled
    results: list[dict] = field(default_factory=list)
    item_progress: dict[str, dict] = field(default_factory=dict)
    progress_events: dict[str, list[dict]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False)
    execution: asyncio.Task[Any] | None = field(default=None, repr=False)
    item_executions: dict[str, asyncio.Task[Any]] = field(default_factory=dict, repr=False)
    single_api_attempts: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    single_api_semaphore: asyncio.Semaphore | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    done_total: int = 0
    error: str | None = None
    active_rerun: dict[str, Any] | None = None
    rerun_history: list[dict[str, Any]] = field(default_factory=list)
    event_cursor: int = 0
    event_log: list[dict] = field(default_factory=list, repr=False)
    last_persist_at: float = field(default=0.0, repr=False)
    last_accessed_at: float = field(default_factory=time.monotonic, repr=False)

    async def publish(self, event: str, data: dict) -> None:
        self.publish_nowait(event, data)

    def publish_nowait(self, event: str, data: dict) -> None:
        self.event_cursor += 1
        message = {
            "event": event,
            "data": data,
            "cursor": self.event_cursor,
        }
        self.event_log.append(message)
        if len(self.event_log) > MAX_EVENT_LOG_SIZE:
            del self.event_log[:-MAX_EVENT_LOG_SIZE]
        if event in _TERMINAL_EVENTS and len(self.event_log) > MAX_TERMINAL_EVENT_LOG_SIZE:
            del self.event_log[:-MAX_TERMINAL_EVENT_LOG_SIZE]
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # 极慢客户端只丢弃 Web 实时投影；完整进度仍已写入任务快照。
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_SSE_QUEUE_SIZE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def mark_started(self, now: float | None = None) -> None:
        """记录批跑真正开始执行的时间，重复调用不覆盖首次时间。"""
        if self.started_at is None:
            self.started_at = time.time() if now is None else now
        self.finished_at = None
        self.duration_s = None
        self.touch()

    def mark_finished(self, now: float | None = None) -> None:
        """记录批跑终止时间和实际墙钟耗时。"""
        self.finished_at = time.time() if now is None else now
        if self.started_at is not None:
            self.duration_s = round(max(0.0, self.finished_at - self.started_at), 3)
        self.touch()

    def touch(self) -> None:
        self.last_accessed_at = time.monotonic()

    def elapsed_s(self, now: float | None = None) -> float | None:
        """返回已结束或运行中的批跑墙钟耗时。"""
        if self.duration_s is not None:
            return self.duration_s
        if self.started_at is None:
            return None
        end = self.finished_at
        if end is None:
            end = time.time() if now is None else now
        return round(max(0.0, end - self.started_at), 3)


TASKS: dict[str, Task] = {}


def _task_is_active(task: Task) -> bool:
    if task.status in _ACTIVE_STATUSES:
        return True
    if task.execution is not None and not task.execution.done():
        return True
    if any(not execution.done() for execution in task.item_executions.values()):
        return True
    return bool(task.subscribers)


def prune_task_cache(
    *,
    now: float | None = None,
    keep_task_ids: set[str] | None = None,
) -> list[str]:
    """淘汰已结束任务的内存对象；磁盘快照不受影响。"""
    current = time.monotonic() if now is None else now
    keep = keep_task_ids or set()
    max_completed = _nonnegative_env_number(
        "AUTO_EVAL_MAX_CACHED_COMPLETED_TASKS",
        str(MAX_CACHED_COMPLETED_TASKS),
        int,
    )
    ttl_s = _nonnegative_env_number(
        "AUTO_EVAL_COMPLETED_TASK_CACHE_TTL_S",
        str(COMPLETED_TASK_CACHE_TTL_S),
        float,
    )
    protected_completed = [
        task for task in list(TASKS.values())
        if task.id in keep and not _task_is_active(task)
    ]
    completed = [
        task for task in list(TASKS.values())
        if task.id not in keep and not _task_is_active(task)
    ]
    completed.sort(key=lambda task: task.last_accessed_at, reverse=True)
    remaining_capacity = max(
        0,
        max_completed - len(protected_completed),
    )
    retained_ids = {
        task.id for task in completed[:remaining_capacity]
    }
    removed: list[str] = []
    for task in completed:
        expired = (
            ttl_s > 0
            and current - task.last_accessed_at > ttl_s
        )
        if task.id in retained_ids and not expired:
            continue
        if TASKS.pop(task.id, None) is not None:
            removed.append(task.id)
    return removed


def remove_task(task_id: str) -> Task | None:
    """显式释放一个非运行中任务；主要供删除历史记录使用。"""
    task = TASKS.get(task_id)
    if task is not None:
        if task.status in _ACTIVE_STATUSES:
            return None
        if task.execution is not None and not task.execution.done():
            return None
        if any(not execution.done() for execution in task.item_executions.values()):
            return None
    return TASKS.pop(task_id, None)


def new_task(
    mode: str,
    items: list[dict],
    options: dict,
    dataset_name: str = "",
    *,
    task_id: str | None = None,
) -> Task:
    prune_task_cache()
    task_id = task_id or uuid.uuid4().hex[:12]
    created_at = time.time()
    t = Task(
        id=task_id,
        mode=mode,
        items=items,
        options=options,
        dataset_name=dataset_name,
        session_name=make_session_name(created_at, mode, task_id),
        created_at=created_at,
    )
    TASKS[task_id] = t
    save_task(t)
    return t


def get_task(task_id: str, *, cache: bool = True) -> Task | None:
    task = TASKS.get(task_id)
    if task:
        task.touch()
        return task
    snapshot = load_snapshot(task_id)
    if not snapshot:
        return None
    status = snapshot.get("status") or "done"
    error = snapshot.get("error")
    active_rerun = snapshot.get("active_rerun")
    rerun_history = list(snapshot.get("rerun_history") or [])
    recovered_rerun = False
    if status == "rerunning":
        recovered_rerun = True
        attempt = dict(active_rerun or {})
        attempt.update({
            "status": "interrupted",
            "finished_at": time.time(),
            "error": attempt.get("error") or "服务中断，重跑已停止；已合并的结果予以保留",
        })
        started_at = attempt.get("started_at")
        if started_at is not None:
            attempt["duration_s"] = round(
                max(0.0, attempt["finished_at"] - float(started_at)), 3,
            )
        rerun_history.append(attempt)
        status = str(attempt.get("base_status") or "done")
        if status not in {"done", "error", "cancelled"}:
            status = "done"
        active_rerun = None
    elif status in {"pending", "running"}:
        status = "error"
        error = error or "服务中断，已保留中断前完成的评估结果"
    task = Task(
        id=snapshot.get("task_id") or task_id,
        mode=snapshot.get("mode") or "single",
        items=snapshot.get("items") or [],
        options=snapshot.get("options") or {},
        dataset_name=snapshot.get("dataset_name") or "",
        note=snapshot.get("note") or "",
        session_name=snapshot.get("session_name") or "",
        judge_trace_path=snapshot.get("judge_trace_path") or "",
        status=status,
        results=snapshot.get("results") or [],
        item_progress=snapshot.get("item_progress") or {},
        progress_events=snapshot.get("progress_events") or {},
        summary=snapshot.get("summary") or {},
        created_at=float(snapshot.get("created_at") or time.time()),
        started_at=(
            float(snapshot["started_at"])
            if snapshot.get("started_at") is not None
            else None
        ),
        finished_at=(
            float(snapshot["finished_at"])
            if snapshot.get("finished_at") is not None
            else None
        ),
        duration_s=(
            float(snapshot["duration_s"])
            if snapshot.get("duration_s") is not None
            else None
        ),
        done_total=int(snapshot.get("done_total") or len(snapshot.get("results") or [])),
        error=error,
        active_rerun=active_rerun,
        rerun_history=rerun_history,
        event_cursor=int(snapshot.get("event_cursor") or 0),
    )
    task.touch()
    if cache:
        TASKS[task.id] = task
        prune_task_cache(keep_task_ids={task.id})
    if recovered_rerun:
        save_task(task)
    return task


def get_live_task(task_id: str) -> Task | None:
    """只返回当前服务进程中仍由任务管理器持有的任务。"""
    return TASKS.get(task_id)
