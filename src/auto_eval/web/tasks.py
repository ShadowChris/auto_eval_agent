"""任务管理：内存存储 + 订阅制 SSE 事件分发。"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .history import load_snapshot, make_session_name, save_task

# 每个 SSE 连接的事件队列上限：慢消费者丢最旧保最新，杜绝无消费者时无限堆积
_MAX_SUB_QUEUE = 500


@dataclass
class Task:
    id: str
    mode: str
    items: list[dict]
    options: dict
    session_name: str = ""
    dataset_name: str = ""
    note: str = ""
    status: str = "pending"  # pending | running | done | error
    results: list[dict] = field(default_factory=list)
    item_progress: dict[str, dict] = field(default_factory=dict)
    progress_events: dict[str, list[dict]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    done_total: int = 0
    error: str | None = None
    # 更新批正在评测的 items 下标（运行时状态，不进快照），供单条查询给 evaluating 标志
    in_flight_indexes: set[int] = field(default_factory=set)
    # SSE 订阅者（每连接独立有界队列）与丢帧计数。订阅制取代旧的单条共享
    # queue：无消费者时不再堆积事件，多连接也各自收到全量而非互相瓜分。
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    dropped_events: int = 0
    # 正在执行的 run_eval / run_update_batch 数（运行时状态，不进快照）。
    # 退休判定用计数而非 status：run_update_batch(manage_status=False)
    # 全程 status=done，只有计数能 pin 住运行中的任务对象。
    active_runs: int = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_SUB_QUEUE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _fanout(self, event: str, data: dict) -> None:
        """同步扇出到全部订阅者；满则丢最旧再放（终态事件必须送达）。

        同步实现是因为 _record_progress 可能经 call_soon_threadsafe 上环，
        不能 await。无订阅者时是空循环——事件不堆积。
        """
        msg = {"event": event, "data": data}
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
                self.dropped_events += 1

    async def publish(self, event: str, data: dict) -> None:
        self._fanout(event, data)


TASKS_CAPACITY = 8  # 常驻内存的任务对象上限（LRU 兜底；运行中任务不会被淘汰）

# 运行中任务必须驻留（SSE/轮询/更新批共享同一活对象）；终态任务在运行结束后
# 退休（retire_task），注册表整体受 LRU 容量兜底——内存不再随任务数/历史
# 浏览数无限增长，miss 时由 get_task 从磁盘快照回载。
TASKS: OrderedDict[str, Task] = OrderedDict()


def _enforce_capacity() -> None:
    """LRU 容量兜底：从最旧开始淘汰空闲终态任务；全是运行中则不强制。"""
    while len(TASKS) > TASKS_CAPACITY:
        for tid, t in TASKS.items():
            if t.active_runs <= 0 and t.status in {"done", "error"}:
                TASKS.pop(tid, None)
                break
        else:
            return


def new_task(
    mode: str,
    items: list[dict],
    options: dict,
    dataset_name: str = "",
    *,
    task_id: str = "",
) -> Task:
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
    _enforce_capacity()
    save_task(t)
    return t


def merge_items_by_id(
    task: Task, incoming: list[dict]
) -> tuple[list[tuple[int, dict]], list[str], list[str]]:
    """按显式 id 合并更新批 items：命中则 incoming dict 整体替换 task.items[i]
    （同下标），未命中追加到末尾。仅携带非空字符串 id 的存量条目参与匹配；
    存量重复 id 以最早位置为准。返回 (批内 (index, item) 按提交顺序列表,
    replaced_ids, added_ids)。函数内无 await，单请求内原子。"""
    id_to_index: dict[str, int] = {}
    for i, it in enumerate(task.items):
        iid = it.get("id")
        if isinstance(iid, str) and iid:
            id_to_index.setdefault(iid, i)
    replaced: list[str] = []
    added: list[str] = []
    batch: list[tuple[int, dict]] = []
    for raw in incoming:  # id 已由 server 层校验：非空字符串且批内唯一
        iid = raw["id"]
        if iid in id_to_index:
            idx = id_to_index[iid]
            task.items[idx] = raw  # 全量替换：新 dict，无旧 frames/历史总结
            replaced.append(iid)
        else:
            idx = len(task.items)
            task.items.append(raw)
            id_to_index[iid] = idx
            added.append(iid)
        batch.append((idx, task.items[idx]))
    return batch, replaced, added


def upsert_result_by_index(task: Task, res: dict) -> str:
    """按 res["index"] 从后往前找同 index 旧结果并整体替换；找不到则追加。
    从后往前保证命中"最近一次"写入（历史里可能存在同 index 重复条目）。
    返回 "replaced" | "appended"。"""
    idx = res.get("index")
    for pos in range(len(task.results) - 1, -1, -1):
        if task.results[pos].get("index") == idx:
            task.results[pos] = res
            return "replaced"
    task.results.append(res)
    return "appended"


def _task_from_snapshot(snapshot: dict, task_id: str) -> Task:
    """从磁盘快照构建 Task 对象（含 pending/running→error 的中断修正）。"""
    status = snapshot.get("status") or "done"
    error = snapshot.get("error")
    if status in {"pending", "running"}:
        status = "error"
        error = error or "服务中断，已保留中断前完成的评估结果"
    return Task(
        id=snapshot.get("task_id") or task_id,
        mode=snapshot.get("mode") or "rich_content",
        items=snapshot.get("items") or [],
        options=snapshot.get("options") or {},
        dataset_name=snapshot.get("dataset_name") or "",
        note=snapshot.get("note") or "",
        session_name=snapshot.get("session_name") or "",
        status=status,
        results=snapshot.get("results") or [],
        item_progress=snapshot.get("item_progress") or {},
        progress_events=snapshot.get("progress_events") or {},
        summary=snapshot.get("summary") or {},
        created_at=float(snapshot.get("created_at") or time.time()),
        # done_total=0 是合法状态（更新批语义就是不动 done_total），显式 0
        # 必须保留；仅在字段缺失（legacy 快照）时才回退到 len(results)。
        done_total=(
            int(snapshot["done_total"])
            if snapshot.get("done_total") is not None
            else len(snapshot.get("results") or [])
        ),
        error=error,
    )


def get_task(task_id: str) -> Task | None:
    """注册视图：miss 时从快照回载并注册进 TASKS（增量更新 API 依赖此语义），
    命中即 LRU 触碰；注册后受容量兜底约束。

    仅限事件循环线程调用（会变异 TASKS）；异步端点请用 get_task_async。"""
    task = TASKS.get(task_id)
    if task:
        TASKS.move_to_end(task_id)
        return task
    snapshot = load_snapshot(task_id)
    if not snapshot:
        return None
    task = _task_from_snapshot(snapshot, task_id)
    TASKS[task.id] = task
    _enforce_capacity()
    return task


def peek_task(task_id: str, *, touch: bool = True) -> Task | None:
    """只读视图：命中 TASKS 返回活对象（含运行中），否则从快照构建临时对象
    且不注册——浏览历史/导出不再把整份快照读进内存永久驻留。

    touch=False 供线程池执行的同步端点使用：跳过 move_to_end，保证完全不
    变异 TASKS（dict 读在 GIL 下安全，写入/迭代与循环线程并发不安全）。"""
    task = TASKS.get(task_id)
    if task:
        if touch:
            TASKS.move_to_end(task_id)
        return task
    snapshot = load_snapshot(task_id)
    if not snapshot:
        return None
    return _task_from_snapshot(snapshot, task_id)


async def _load_task_view(task_id: str, *, register: bool) -> Task | None:
    """并发安全的任务视图加载（R6）：磁盘读取放 to_thread 不阻塞事件循环，
    TASKS 触碰/注册/容量控制留在循环线程——线程池端点直接调同步版会在错误
    线程变异共享 OrderedDict，与 move_to_end/_enforce_capacity 迭代竞争。"""
    task = TASKS.get(task_id)
    if task:
        TASKS.move_to_end(task_id)
        return task
    snapshot = await asyncio.to_thread(load_snapshot, task_id)
    if not snapshot:
        return None
    task = _task_from_snapshot(snapshot, task_id)
    if register:
        # to_thread 让出期间可能已有并发注册（如提交端点先回载并 pin）：
        # 复用已注册对象，避免同 id 双对象分叉（运行中的 pin 对象被顶掉）
        existing = TASKS.get(task.id)
        if existing is not None:
            return existing
        TASKS[task.id] = task
        _enforce_capacity()
    return task


async def get_task_async(task_id: str) -> Task | None:
    """get_task 的异步端点版：注册语义一致，见 _load_task_view。"""
    return await _load_task_view(task_id, register=True)


async def peek_task_async(task_id: str) -> Task | None:
    """peek_task 的异步端点版：只读不注册，见 _load_task_view。"""
    return await _load_task_view(task_id, register=False)


def retire_task(task: Task) -> None:
    """运行结束后的主动退休：空闲且终态才从注册表移除（磁盘快照兜底回载）。

    身份校验防止误删同 id 的新对象（删除历史后重建等场景）。
    """
    if task.active_runs > 0 or task.status not in {"done", "error"}:
        return
    if TASKS.get(task.id) is task:
        TASKS.pop(task.id, None)
