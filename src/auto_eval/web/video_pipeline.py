"""有界阶段调度：下载独立线程池，抽帧沿用原线程池，模型仍异步执行。

不建立全批次屏障，不在历史/结果中持久化临时视频路径。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import AsyncIterator, Callable, Iterator, TypeVar

from ..observability import current_context, log_event
from ..paths import RUNS_DIR
from .pipeline_config import current_pipeline_config as load_pipeline_config
from . import remote_video


T = TypeVar("T")
logger = logging.getLogger(__name__)
DEFAULT_EXTRACTION_LIMIT = 12
_REFERENCE_CONCURRENCY = 35
_REFERENCE_EXTRACTIONS = 12
_REFERENCE_HANDOFF_CAPACITY = 20
_SUPPLY_CHECK_INTERVAL = 15
_DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=128, thread_name_prefix="video-download")
_COUNTS: dict[str, int] = {}
_LOCK = threading.Lock()
_SCOPE: ContextVar[list | None] = ContextVar("video_item_resources", default=None)
_WORK: ContextVar[list | None] = ContextVar("video_stage_workers", default=None)


def _stage_limit(stage: str) -> int:
    limit = getattr(load_pipeline_config(), f"{stage}_concurrency")
    if stage == "extraction" and not limit:
        return DEFAULT_EXTRACTION_LIMIT
    return limit


def _model_slot_available() -> bool:
    limit = _stage_limit("model")
    with _LOCK:
        return not limit or _COUNTS.get("model", 0) < limit


@dataclass(eq=False)
class _ExtractionWaiter:
    future: asyncio.Future[float]
    granted: bool = False


class _ExtractionQueue:
    """Round-robin by task, FIFO within each task, under the process stage lock."""

    def __init__(self) -> None:
        # The inner dict is insertion ordered and allows cancellation without scanning the queue.
        self.groups: OrderedDict[str | None, dict[_ExtractionWaiter, None]] = OrderedDict()
        self.last_group: str | None = None

    @staticmethod
    def _deliver(future: asyncio.Future[float], granted_at: float) -> None:
        if not future.done():
            future.set_result(granted_at)

    def dispatch_locked(self) -> None:
        if not self.groups:
            return
        limit = _stage_limit("extraction")
        while self.groups and _COUNTS.get("extraction", 0) < limit:
            # Give a newly arrived group a turn if the previous group had the pool to itself.
            if len(self.groups) > 1 and next(iter(self.groups)) == self.last_group:
                self.groups.move_to_end(self.last_group)
            group, members = self.groups.popitem(last=False)
            waiter = next(iter(members))
            members.pop(waiter)
            if members:
                self.groups[group] = members
            try:
                waiter.future.get_loop().call_soon_threadsafe(self._deliver, waiter.future, time.monotonic())
            except RuntimeError:  # Its event loop has already closed.
                continue
            waiter.granted = True
            _COUNTS["extraction"] = _COUNTS.get("extraction", 0) + 1
            self.last_group = group

    async def acquire(self, group: str | None) -> float:
        queued_at = time.monotonic()
        waiter = _ExtractionWaiter(asyncio.get_running_loop().create_future())
        with _LOCK:
            self.groups.setdefault(group, {})[waiter] = None
            self.dispatch_locked()
        try:
            granted_at = await waiter.future
        except asyncio.CancelledError:
            with _LOCK:
                if waiter.granted:
                    # A slot can be reserved before the awaiting task resumes.
                    _COUNTS["extraction"] -= 1
                else:
                    members = self.groups.get(group)
                    if members is not None:
                        members.pop(waiter, None)
                        if not members:
                            del self.groups[group]
                self.dispatch_locked()
            raise
        return granted_at - queued_at


_EXTRACTION_QUEUE = _ExtractionQueue()


def refresh_extraction_slots() -> None:
    """Wake queued extractors after the Web service has saved a new limit."""
    with _LOCK:
        _EXTRACTION_QUEUE.dispatch_locked()


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[(len(ordered) * 95 + 99) // 100 - 1] if ordered else 0.0


class _PreparationTicket:
    def __init__(self, batch: OperationBatch) -> None:
        self.batch = batch
        self.reserved = True
        self.ready = False

    def release_handoff(self) -> None:
        # The first attempt releases the handoff position. Model retries use the same frames.
        if self.reserved:
            self.reserved = False
            if self.ready:
                self.batch.ready -= 1
            else:
                self.batch.preparing -= 1
            self.batch._handoff.release()


class OperationBatch:
    """Bound pending evidence separately from model work, sharing process stage limits."""

    def __init__(self, concurrency: int, item_count: int) -> None:
        self.model_limit = max(1, min(concurrency, item_count))
        n = min(self.model_limit, _stage_limit("model") or self.model_limit)
        self.capacity = self._scale(n, _REFERENCE_HANDOFF_CAPACITY)
        self.extraction_target = min(self._scale(n, _REFERENCE_EXTRACTIONS), _stage_limit("extraction"))
        self._handoff = asyncio.BoundedSemaphore(self.capacity)
        # The live process limit may change after the batch has started.
        self._models = asyncio.BoundedSemaphore(self.model_limit)
        self._extraction_release = asyncio.Event()
        self.waiting = 0
        self.preparing = 0
        self.ready = 0
        self.models = 0
        self.extractions = 0
        self.extractions_admitted = 0
        self.waiting_for_extraction = 0
        self.extraction_times: list[float] = []
        self.extraction_wait_times: list[float] = []
        self.peak_ready = 0
        self.model_starved_s = 0
        self._supply_checks = 0

    @staticmethod
    def _scale(concurrency: int, reference: int) -> int:
        return (reference * concurrency + _REFERENCE_CONCURRENCY - 1) // _REFERENCE_CONCURRENCY

    @asynccontextmanager
    async def reserve(self) -> AsyncIterator[_PreparationTicket]:
        self.waiting += 1
        try:
            await self._handoff.acquire()
        finally:
            self.waiting -= 1
        ticket = _PreparationTicket(self)
        self.preparing += 1
        try:
            yield ticket
        finally:
            ticket.release_handoff()

    @asynccontextmanager
    async def model(self, ticket: _PreparationTicket) -> AsyncIterator[None]:
        ticket.ready = True
        self.preparing -= 1
        self.ready += 1
        self.peak_ready = max(self.peak_ready, self.ready)
        async with self._models:
            self.models += 1
            try:
                yield
            finally:
                self.models -= 1

    async def acquire_extraction(self) -> None:
        self.waiting_for_extraction += 1
        try:
            while self.extractions_admitted >= self.extraction_target:
                await self._extraction_release.wait()
            self.extractions_admitted += 1
        finally:
            self.waiting_for_extraction -= 1

    def _wake_extraction(self) -> None:
        self._extraction_release.set()
        self._extraction_release = asyncio.Event()

    def release_extraction(self, started: float | None) -> None:
        self.extractions_admitted -= 1
        if started is not None:
            self.extractions -= 1
            self.extraction_times.append(time.monotonic() - started)
        self._wake_extraction()

    def _needs_frames(self) -> bool:
        return (
            self.ready == 0 and (self.waiting > 0 or self.preparing > 0)
            and self.models < self.model_limit and _model_slot_available()
        )

    def check_supply(self) -> None:
        limit = min(_stage_limit("extraction"), self.capacity)
        if (self._needs_frames() and self.extractions >= self.extraction_target
                and (self.waiting > 0 or self.waiting_for_extraction > 0)):
            self._supply_checks += 1
            if self._supply_checks >= 2 and self.extraction_target < limit:
                self.extraction_target += 1
                self._supply_checks = 0
                self._wake_extraction()
        else:
            self._supply_checks = 0

    async def observe(self) -> None:
        seconds = 0
        while True:
            await asyncio.sleep(1)
            seconds += 1
            self.model_starved_s += int(self._needs_frames())
            if seconds % _SUPPLY_CHECK_INTERVAL == 0:
                self.check_supply()

    def report(self, task_id: str) -> None:
        logger.info(
            "任务类调度 task_id=%s ready_capacity=%d peak_ready=%d extraction_target=%d "
            "extraction_calls=%d extraction_wait_p95_s=%.2f extraction_p95_s=%.2f "
            "model_waiting_for_frames_s=%d",
            task_id, self.capacity, self.peak_ready, self.extraction_target,
            len(self.extraction_times), _p95(self.extraction_wait_times),
            _p95(self.extraction_times), self.model_starved_s,
        )


@asynccontextmanager
async def stage_slot(
    stage: str, *, enabled: bool = True, batch: OperationBatch | None = None,
) -> AsyncIterator[None]:
    """跨任务、跨事件循环共享的进程上限；排队不占工作线程。"""
    acquired = False
    batch_acquired = False
    started = None
    workers: list = []
    token = _WORK.set(workers)
    try:
        if enabled:
            if batch is not None:
                await batch.acquire_extraction()
                batch_acquired = True
            limit = _stage_limit(stage)
            log_event("阶段调度", "等待资源", details={"阶段": stage, "并发上限": limit})
            if stage == "extraction":
                task_id = current_context().task_id
                wait_s = await _EXTRACTION_QUEUE.acquire(task_id if task_id and task_id != "-" else None)
                acquired = True
                if batch is not None:
                    batch.extraction_wait_times.append(wait_s)
            else:
                while not acquired:
                    limit = _stage_limit(stage)
                    with _LOCK:
                        if not limit or _COUNTS.get(stage, 0) < limit:
                            _COUNTS[stage] = _COUNTS.get(stage, 0) + 1
                            acquired = True
                    if not acquired:
                        await asyncio.sleep(0.05)
            if batch is not None:
                batch.extractions += 1
                started = time.monotonic()
        yield
    finally:
        _WORK.reset(token)
        if acquired or batch_acquired:
            pending = [worker for worker in workers if not worker.done()]
            released = False

            def release(_done=None) -> None:
                nonlocal released
                with _LOCK:
                    if not released and all(worker.done() for worker in pending):
                        if acquired:
                            _COUNTS[stage] -= 1
                            if stage == "extraction":
                                _EXTRACTION_QUEUE.dispatch_locked()
                        released = True
                    else:
                        return
                if batch_acquired:
                    batch.release_extraction(started)

            # 超时/取消不会终止已运行的线程，真实退出前继续占用阶段槽。
            for worker in pending:
                worker.add_done_callback(release)
            release()


class _VideoLease:
    """单题拥有临时文件；取消后等仍在运行的线程退出，才实际删除。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0
        self.closed = False
        self.manager = None

    def retain(self) -> None:
        with self.lock:
            # An in-flight import can transfer its pin to the worker after the request closes.
            if self.closed and self.users == 0:
                raise RuntimeError("视频资源已释放")
            self.users += 1

    def release(self) -> None:
        with self.lock:
            self.users -= 1
        self._cleanup()

    def close(self) -> None:
        with self.lock:
            self.closed = True
        self._cleanup()

    def _cleanup(self) -> None:
        with self.lock:
            manager = self.manager if self.closed and self.users == 0 else None
            if manager is not None:
                self.manager = None
        if manager is not None:
            manager.__exit__(None, None, None)

    def download(self, raw: str, runs_dir: Path, *, record_digest: Callable[[str], None] | None = None) -> Path:
        manager = remote_video.local_video_input(raw, runs_dir, record_digest=record_digest)
        path = manager.__enter__()
        with self.lock:
            self.manager = manager
        return path


@contextmanager
def video_item_scope() -> Iterator[None]:
    """覆盖准备、模型重试和结果落盘；不删除用户本地原视频。"""
    leases: list[_VideoLease] = []
    token = _SCOPE.set(leases)
    try:
        yield
    finally:
        _SCOPE.reset(token)
        for lease in leases:
            lease.close()


async def run_video_worker(
    function: Callable[[], T], *, executor=None, lease: _VideoLease | None = None,
) -> T:
    context = copy_context()
    if lease is not None:
        lease.retain()

    def work() -> T:
        try:
            cancel = remote_video._CANCEL.get()
            if cancel is not None and cancel.is_set():
                raise ValueError("视频准备已取消")
            return function()
        finally:
            if lease is not None:
                lease.release()

    try:
        future = asyncio.get_running_loop().run_in_executor(executor, context.run, work)
    except BaseException:
        if lease is not None:
            lease.release()
        raise
    # 取消调用者不会取消排队/运行中的线程，确保线程的资源引用始终被归还。
    future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    if _WORK.get() is not None:
        _WORK.get().append(future)
    return await asyncio.shield(future)


@asynccontextmanager
async def _download_slot() -> AsyncIterator[None]:
    async with stage_slot("download"):
        token = remote_video._DOWNLOAD_SLOT.set(True)
        try:
            yield
        finally:
            remote_video._DOWNLOAD_SLOT.reset(token)


@asynccontextmanager
async def import_video_input(raw: str, runs_dir: Path) -> AsyncIterator[tuple[Path, str, _VideoLease]]:
    lease = _VideoLease()
    digests: list[str] = []
    stop = threading.Event()
    token = remote_video._CANCEL.set(stop)
    try:
        async with _download_slot():
            path = await run_video_worker(
                lambda: lease.download(raw, runs_dir, record_digest=digests.append),
                executor=_DOWNLOAD_POOL, lease=lease,
            )
        yield path, digests[0], lease
    finally:
        stop.set()
        remote_video._CANCEL.reset(token)
        lease.close()


@dataclass
class _ImportExtraction:
    task: asyncio.Task[list[Path]]
    started: asyncio.Event
    waiters: int = 0


_IMPORT_EXTRACTIONS: dict[tuple[asyncio.AbstractEventLoop, Path], _ImportExtraction] = {}


async def extract_import_frames(
    frame_dir: Path, load_cache: Callable[[], list[Path]], extract: Callable[[], list[Path]],
    *, lease: _VideoLease | None = None,
) -> list[Path]:
    """One worker per import cache directory. Request cancellation only detaches its own waiter."""
    key = (asyncio.get_running_loop(), frame_dir)
    flight = _IMPORT_EXTRACTIONS.get(key)
    if flight is None:
        started = asyncio.Event()

        async def work() -> list[Path]:
            stop = threading.Event()
            token = remote_video._CANCEL.set(stop)
            try:
                cached = await run_video_worker(load_cache)
                if cached:
                    return cached
                async with stage_slot("extraction"):
                    started.set()
                    return await run_video_worker(extract, lease=lease)
            finally:
                stop.set()
                remote_video._CANCEL.reset(token)

        # Pin the leading request's video before its own cancellation can close the lease.
        if lease is not None:
            lease.retain()
        task = asyncio.create_task(work())
        flight = _ImportExtraction(task, started)
        _IMPORT_EXTRACTIONS[key] = flight

        def finish(done: asyncio.Task[list[Path]]) -> None:
            if _IMPORT_EXTRACTIONS.get(key) is flight:
                del _IMPORT_EXTRACTIONS[key]
            if lease is not None:
                lease.release()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finish)
    flight.waiters += 1
    try:
        return await asyncio.shield(flight.task)
    finally:
        flight.waiters -= 1
        if not flight.waiters and not flight.started.is_set() and not flight.task.done():
            if _IMPORT_EXTRACTIONS.get(key) is flight:
                del _IMPORT_EXTRACTIONS[key]
            flight.task.cancel()


async def prepare_video(
    function: Callable[..., T], *args, preparation_timeout_s: float | None = None,
    operation_pipeline: bool = True, operation_batch: OperationBatch | None = None, **kwargs,
) -> T:
    """兼容原同步准备函数；下载与抽帧分别准入，原 URL 仍由原函数回填。"""
    if _SCOPE.get() is None:
        with video_item_scope():
            return await prepare_video(
                function, *args, preparation_timeout_s=preparation_timeout_s,
                operation_pipeline=operation_pipeline, operation_batch=operation_batch, **kwargs,
            )
    stop = threading.Event()
    cancel_token = remote_video._CANCEL.set(stop)
    local_token = None
    lease = None
    elapsed = 0.0

    async def execute(call: Callable[[], T], *, executor=None) -> T:
        nonlocal elapsed
        started = time.monotonic()
        try:
            remaining = None if preparation_timeout_s is None else preparation_timeout_s - elapsed
            if remaining is not None and remaining <= 0:
                raise TimeoutError("视频准备超过时间限制")
            return await asyncio.wait_for(run_video_worker(call, executor=executor, lease=lease), remaining)
        finally:
            elapsed += time.monotonic() - started

    try:
        item = args[0] if args and isinstance(args[0], dict) else {}
        media = item.get("media") or []
        raw = str(item.get("video_path") or (media[0] if media else "")).strip()
        if operation_pipeline and remote_video.is_remote_video_url(raw):
            lease = _VideoLease()
            _SCOPE.get().append(lease)
            async with _download_slot():
                local = await execute(
                    lambda: lease.download(raw, kwargs.get("runs_dir", RUNS_DIR)),
                    executor=_DOWNLOAD_POOL,
                )
            local_token = remote_video._PREPARED_INPUT.set((raw, local))
        async with stage_slot("extraction", enabled=operation_pipeline, batch=operation_batch):
            log_event("视频准备", "开始抽帧阶段", progress=5, progress_message="正在校验视频并抽帧")
            return await execute(lambda: function(*args, **kwargs))
    finally:
        stop.set()
        if local_token is not None:
            remote_video._PREPARED_INPUT.reset(local_token)
        remote_video._CANCEL.reset(cancel_token)
