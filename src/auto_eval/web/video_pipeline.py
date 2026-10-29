"""有界阶段调度：下载独立线程池，抽帧沿用原线程池，模型仍异步执行。

不建立全批次屏障，不在历史/结果中持久化临时视频路径。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
import threading
import time
from typing import AsyncIterator, Callable, Iterator, TypeVar

from ..observability import log_event
from ..paths import RUNS_DIR
from .pipeline_config import current_pipeline_config as load_pipeline_config
from . import remote_video


T = TypeVar("T")
_DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=128, thread_name_prefix="video-download")
_COUNTS: dict[str, int] = {}
_LOCK = threading.Lock()
_SCOPE: ContextVar[list | None] = ContextVar("video_item_resources", default=None)
_WORK: ContextVar[list | None] = ContextVar("video_stage_workers", default=None)


@asynccontextmanager
async def stage_slot(stage: str, *, enabled: bool = True) -> AsyncIterator[None]:
    """跨任务、跨事件循环共享的进程上限；排队不占工作线程。"""
    acquired = False
    workers: list = []
    token = _WORK.set(workers)
    try:
        if enabled:
            limit = getattr(load_pipeline_config(), f"{stage}_concurrency")
            log_event("阶段调度", "等待资源", details={"阶段": stage, "并发上限": limit})
            while not acquired:
                # 每次准入都重读当前快照，排队中的任务也能响应 Web 热更新。
                limit = getattr(load_pipeline_config(), f"{stage}_concurrency")
                with _LOCK:
                    if not limit or _COUNTS.get(stage, 0) < limit:
                        _COUNTS[stage] = _COUNTS.get(stage, 0) + 1
                        acquired = True
                if not acquired:
                    await asyncio.sleep(0.05)
        yield
    finally:
        _WORK.reset(token)
        if acquired:
            pending = [worker for worker in workers if not worker.done()]
            released = False

            def release(_done=None) -> None:
                nonlocal released
                with _LOCK:
                    if not released and all(worker.done() for worker in pending):
                        _COUNTS[stage] -= 1
                        released = True

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
            if self.closed:
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

    def download(self, raw: str, runs_dir: Path) -> Path:
        manager = remote_video.local_video_input(raw, runs_dir)
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


async def _thread_call(
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


async def prepare_video(
    function: Callable[..., T], *args, preparation_timeout_s: float | None = None,
    operation_pipeline: bool = True, **kwargs,
) -> T:
    """兼容原同步准备函数；下载与抽帧分别准入，原 URL 仍由原函数回填。"""
    if _SCOPE.get() is None:
        with video_item_scope():
            return await prepare_video(
                function, *args, preparation_timeout_s=preparation_timeout_s,
                operation_pipeline=operation_pipeline, **kwargs,
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
            return await asyncio.wait_for(_thread_call(call, executor=executor, lease=lease), remaining)
        finally:
            elapsed += time.monotonic() - started

    try:
        item = args[0] if args and isinstance(args[0], dict) else {}
        media = item.get("media") or []
        raw = str(item.get("video_path") or (media[0] if media else "")).strip()
        if operation_pipeline and remote_video.is_remote_video_url(raw):
            lease = _VideoLease()
            _SCOPE.get().append(lease)
            async with stage_slot("download"):
                slot_token = remote_video._DOWNLOAD_SLOT.set(True)
                try:
                    local = await execute(
                        lambda: lease.download(raw, kwargs.get("runs_dir", RUNS_DIR)),
                        executor=_DOWNLOAD_POOL,
                    )
                finally:
                    remote_video._DOWNLOAD_SLOT.reset(slot_token)
            local_token = remote_video._PREPARED_INPUT.set((raw, local))
        async with stage_slot("extraction", enabled=operation_pipeline):
            log_event("视频准备", "开始抽帧阶段", progress=5, progress_message="正在校验视频并抽帧")
            return await execute(lambda: function(*args, **kwargs))
    finally:
        stop.set()
        if local_token is not None:
            remote_video._PREPARED_INPUT.reset(local_token)
        remote_video._CANCEL.reset(cancel_token)
