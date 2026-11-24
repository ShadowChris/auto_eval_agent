from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import threading
import time

import pytest

from auto_eval.config import load_config
from auto_eval.observability import bind_chain_context
from auto_eval.web import operation_media, pipeline_config, remote_video, runner, server, video_pipeline
from auto_eval.web.pipeline_config import OperationPipelineConfig
from auto_eval.web.tasks import Task


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(0.005)


@pytest.fixture
def limits(monkeypatch):
    def configure(**kwargs):
        config = replace(OperationPipelineConfig(), **kwargs)
        monkeypatch.setattr(video_pipeline, "load_pipeline_config", lambda: config)
        return config
    return configure


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    paths = []

    def download(_, directory):
        path = directory / "video.mp4"
        path.write_bytes(b"video")
        paths.append(path)
        return path

    monkeypatch.setattr(remote_video, "download_remote_video", download)
    monkeypatch.setattr(video_pipeline, "RUNS_DIR", tmp_path / "runs")
    return paths


def test_pipeline_defaults_and_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_config, "CONFIG_PATH", tmp_path / "missing.yaml")
    pipeline_config.load_pipeline_config.cache_clear()
    try:
        assert pipeline_config.load_pipeline_config() == OperationPipelineConfig(10, 0, 0)
    finally:
        pipeline_config.load_pipeline_config.cache_clear()


@pytest.mark.parametrize("value", [
    "download_concurrency: 0", "download_concurrency: 129", "download_concurrency: true",
    "extraction_concurrency: -1", "extraction_concurrency: 1.5",
    "model_concurrency: '2'", "unknown: 1", "- invalid", "false", "[]",
])
def test_invalid_pipeline_configuration_is_rejected(tmp_path, monkeypatch, value):
    path = tmp_path / "pipeline.yaml"
    path.write_text(value, encoding="utf-8")
    monkeypatch.setattr(pipeline_config, "CONFIG_PATH", path)
    pipeline_config.load_pipeline_config.cache_clear()
    try:
        with pytest.raises(ValueError):
            pipeline_config.load_pipeline_config()
    finally:
        pipeline_config.load_pipeline_config.cache_clear()


@pytest.mark.asyncio
async def test_stages_overlap_with_independent_shared_limits(tmp_path, monkeypatch, limits):
    limits(download_concurrency=3, extraction_concurrency=2, model_concurrency=1)
    active = {"download": 0, "extract": 0, "model": 0}
    peak = active.copy()
    lock = threading.Lock()
    paths = []
    download_threads = []
    extraction_threads = []
    overlapped = []

    def enter(stage):
        with lock:
            active[stage] += 1
            peak[stage] = max(peak[stage], active[stage])
            overlapped.append(sum(value > 0 for value in active.values()) > 1)

    def leave(stage):
        with lock:
            active[stage] -= 1

    def download(_, directory):
        enter("download")
        try:
            download_threads.append(threading.current_thread().name)
            time.sleep(0.015)
            path = directory / "video.mp4"
            path.write_bytes(b"video")
            paths.append(path)
            return path
        finally:
            leave("download")

    def extract(item):
        enter("extract")
        try:
            extraction_threads.append(threading.current_thread().name)
            with remote_video.local_video_input(item["video_path"], tmp_path) as path:
                assert path.is_file()
                time.sleep(0.03)
            return {**item, "frames": ["frame.jpg"]}
        finally:
            leave("extract")

    monkeypatch.setattr(remote_video, "download_remote_video", download)

    async def item(index):
        with video_pipeline.video_item_scope():
            prepared = await video_pipeline.prepare_video(
                extract, {"id": str(index), "video_path": f"https://cdn.test/{index}"},
                preparation_timeout_s=3,
            )
            assert prepared["video_path"] == f"https://cdn.test/{index}"
            async with video_pipeline.stage_slot("model"):
                enter("model")
                try:
                    await asyncio.sleep(0.01)
                finally:
                    leave("model")

    monkeypatch.setattr(video_pipeline, "RUNS_DIR", tmp_path)
    # 两个独立任务组同时提交，仍共用同一阶段配额。
    await asyncio.gather(
        asyncio.gather(*(item(i) for i in range(6))),
        asyncio.gather(*(item(i) for i in range(6, 12))),
    )
    assert peak == {"download": 3, "extract": 2, "model": 1}
    assert any(overlapped)
    assert all(name.startswith("video-download") for name in download_threads)
    assert all(not name.startswith("video-download") for name in extraction_threads)
    assert not any(path.exists() for path in paths)
    assert not any(video_pipeline._COUNTS.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_video_retained_through_model_and_cleaned_on_terminal_state(downloads, failed):
    async def run():
        with video_pipeline.video_item_scope():
            await video_pipeline.prepare_video(lambda item: item, {"video_path": "https://cdn.test/a"})
            assert downloads[0].is_file()
            if failed:
                raise ValueError("model failed")
    if failed:
        with pytest.raises(ValueError, match="model failed"):
            await run()
    else:
        await run()
    assert not downloads[0].exists()


@pytest.mark.asyncio
async def test_preparation_without_item_scope_cleans_immediately(downloads):
    result = await remote_video.run_video_preparation(
        lambda item: item, {"video_path": "https://cdn.test/a", "id": "a"},
    )
    assert result["id"] == "a"
    assert not downloads[0].exists()


@pytest.mark.asyncio
async def test_cancel_during_model_cleans_download(downloads):
    entered = asyncio.Event()

    async def item():
        with video_pipeline.video_item_scope():
            await video_pipeline.prepare_video(lambda item: item, {"video_path": "https://cdn.test/a"})
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(item())
    await entered.wait()
    assert downloads[0].is_file()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not downloads[0].exists()


@pytest.mark.asyncio
async def test_cancelled_extractor_keeps_file_and_slot_until_thread_exits(downloads, limits):
    limits(extraction_concurrency=1)
    batch = video_pipeline.OperationBatch(2, 3)
    entered = threading.Event()
    finish = threading.Event()
    second_entered = threading.Event()

    def extract(item):
        entered.set()
        assert finish.wait(3)
        assert downloads[0].is_file()
        return item

    async def item():
        with video_pipeline.video_item_scope():
            await video_pipeline.prepare_video(
                extract, {"video_path": "https://cdn.test/a"}, operation_batch=batch,
            )

    first = asyncio.create_task(item())
    try:
        await until(entered.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert downloads[0].is_file()
        assert video_pipeline._COUNTS["extraction"] == 1
        assert batch.extractions_admitted == 1
        second = asyncio.create_task(video_pipeline.prepare_video(lambda: second_entered.set()))
        await asyncio.sleep(0.06)
        assert not second_entered.is_set()
    finally:
        finish.set()
    await second
    await until(lambda: not downloads[0].exists())
    assert video_pipeline._COUNTS["extraction"] == 0
    assert batch.extractions_admitted == 0


@pytest.mark.asyncio
async def test_queue_wait_does_not_consume_execution_timeout(limits):
    limits(extraction_concurrency=1)
    async with video_pipeline.stage_slot("extraction"):
        task = asyncio.create_task(video_pipeline.prepare_video(lambda: "done", preparation_timeout_s=0.05))
        await asyncio.sleep(0.12)
        assert not task.done()
    assert await task == "done"


@pytest.mark.asyncio
async def test_cancel_queued_extraction_does_not_start_worker(downloads, limits):
    limits(extraction_concurrency=1)
    calls = []
    async with video_pipeline.stage_slot("extraction"):
        task = asyncio.create_task(video_pipeline.prepare_video(
            lambda item: calls.append(item), {"video_path": "https://cdn.test/a"},
        ))
        await until(lambda: bool(downloads))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await until(lambda: not downloads[0].exists())
    assert not calls
    assert video_pipeline._COUNTS["extraction"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("other_groups", [[], ["B"], ["B", "C", "D"], [None]])
async def test_extraction_rotates_batches_and_preserves_fifo(other_groups, limits):
    limits(extraction_concurrency=1)
    order = []
    workers = []

    async def extract(group, index, started):
        with bind_chain_context(task_id=group or "-"):
            started.set()
            async with video_pipeline.stage_slot("extraction"):
                order.append((group, index))

    try:
        with bind_chain_context(task_id="A"):
            async with video_pipeline.stage_slot("extraction"):
                # A already occupies the pool and has a backlog before the small batches arrive.
                for group, count in [("A", 4)] + [(group, 2) for group in other_groups]:
                    for index in range(count):
                        started = asyncio.Event()
                        workers.append(asyncio.create_task(extract(group, index, started)))
                        await started.wait()
                assert order == []
        await asyncio.wait_for(asyncio.gather(*workers), 3)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    rotation = other_groups + ["A"]
    expected = [(group, index) for index in range(2) for group in rotation] + [("A", 2), ("A", 3)]
    assert order == expected
    assert video_pipeline._COUNTS["extraction"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_cancel_extraction_waiter_before_or_after_slot_is_reserved(after_dispatch, limits):
    limits(extraction_concurrency=1)
    ran = []
    workers = []

    async def extract(task_id, started):
        with bind_chain_context(task_id=task_id):
            started.set()
            async with video_pipeline.stage_slot("extraction"):
                ran.append(task_id)

    try:
        async with video_pipeline.stage_slot("extraction"):
            for task_id in ("A", "B"):
                started = asyncio.Event()
                workers.append(asyncio.create_task(extract(task_id, started)))
                await started.wait()
            if not after_dispatch:
                workers[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await workers[0]
        if after_dispatch:
            # The release reserved a slot for A, but the event loop has not resumed A yet.
            assert video_pipeline._COUNTS["extraction"] == 1
            workers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await workers[0]
        await asyncio.wait_for(workers[1], 3)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    assert ran == ["B"]
    assert video_pipeline._COUNTS["extraction"] == 0


@pytest.mark.asyncio
async def test_local_video_never_deleted_and_zero_extraction_limit_preserves_parallelism(tmp_path, limits):
    limits(extraction_concurrency=0)
    path = tmp_path / "original.mp4"
    path.write_bytes(b"video")
    barrier = threading.Barrier(3)

    def prepare(item):
        barrier.wait(timeout=3)
        return item

    await asyncio.gather(*[
        video_pipeline.prepare_video(prepare, {"video_path": str(path)}) for _ in range(3)
    ])
    assert path.is_file()


@pytest.mark.asyncio
async def test_runner_preserves_url_result_and_reuses_frames_after_first_run(tmp_path, downloads, monkeypatch):
    cfg = load_config(Path("config"))
    raw = "https://cdn.test/video.mp4?token=signed"
    task = Task(
        id="pipeline-regression", mode="operation",
        items=[{"id": "case1", "query": "打开设置", "video_path": raw}],
        options={"concurrency": 35},
    )
    real_prepare = operation_media.prepare_session_operation_item

    def extract(video, directory, **kwargs):
        assert video.is_file()
        frame = directory / "kf_001.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    def prepare(item, **kwargs):
        return real_prepare(
            item, base_dir=tmp_path, runs_dir=tmp_path / "runs",
            probe_fn=lambda _: 10, extract_fn=extract, **kwargs,
        )

    calls = []

    async def evaluate(*args, **kwargs):
        calls.append(args[2]["id"])
        if len(calls) == 1:
            assert downloads[0].is_file()
        assert args[2]["video_path"] == raw
        assert Path(args[2]["frames"][0]).is_file()
        return {"query": "打开设置", "correctness": "ok", "issue_types": [], "total": 5}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)
    await runner._run(task, cfg)
    assert task.results[0].get("correctness") == "ok", task.results[0]
    assert not downloads[0].exists()
    assert task.items[0]["video_path"] == raw
    assert task.items[0]["media"] == [raw]
    await runner._run(task, cfg, item_indices=[0])
    assert len(downloads) == 1
    assert calls == ["case1", "case1"]
    assert task.results[0]["correctness"] == "ok"


@pytest.mark.asyncio
async def test_batch_prefetch_is_bounded_and_long_model_only_holds_its_own_slot(tmp_path, monkeypatch, limits):
    limits()
    prepared = []
    started = []
    release_long = asyncio.Event()
    release_short = asyncio.Event()
    videos = [{"id": str(i), "query": "打开设置", "video_path": "ignored.mp4"} for i in range(6)]
    task = Task(id="bounded-batch", mode="operation", items=videos, options={"concurrency": 2})

    def prepare(item, **kwargs):
        frame = tmp_path / f"{item['id']}.jpg"
        frame.write_bytes(item["id"].encode())
        prepared.append(item["id"])
        return {**item, "frames": [str(frame)], "frame_count": 1}

    async def evaluate(*args, **kwargs):
        item = args[2]
        assert Path(item["frames"][0]).read_bytes() == item["id"].encode()
        started.append(item["id"])
        if item["id"] == "0":
            await release_long.wait()
        elif item["id"] == "1":
            await release_short.wait()
        return {"item_id": item["id"], "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    run = asyncio.create_task(runner._run(task, load_config(Path("config"))))
    try:
        await until(lambda: len(prepared) >= 4)
        assert started == ["0", "1"]
        assert prepared == ["0", "1", "2", "3"]  # 2 in models, 2 reserved for handoff
        release_short.set()
        await until(lambda: len(task.results) == 5)
        assert set(started) == {str(i) for i in range(6)}
        assert not run.done()  # only the long request is still running
        release_long.set()
        await asyncio.wait_for(run, 3)
        assert [row["item_id"] for row in task.results] == [str(i) for i in range(6)]
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_handoff_position_is_held_while_waiting_for_process_model_slot(limits):
    limits(model_concurrency=1)
    batch = video_pipeline.OperationBatch(35, 40)
    entered_model = asyncio.Event()
    next_preparation = asyncio.Event()

    async def evaluate():
        async with batch.reserve() as ticket:
            async with batch.model(ticket):
                async with video_pipeline.stage_slot("model"):
                    ticket.release_handoff()
                    entered_model.set()

    async def prepare_next():
        async with batch.reserve():
            next_preparation.set()

    async with video_pipeline.stage_slot("model"):
        first = asyncio.create_task(evaluate())
        await until(lambda: batch.ready == 1)
        second = asyncio.create_task(prepare_next())
        await until(lambda: batch.waiting == 1)
        assert not entered_model.is_set() and not next_preparation.is_set()
    await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert entered_model.is_set() and next_preparation.is_set()
    assert batch.ready == 0


def test_batch_capacity_scales_and_only_grows_after_two_starvation_checks(limits):
    limits()
    default = video_pipeline.OperationBatch(35, 100)
    assert (default.extraction_target, default.capacity) == (12, 20)
    batch = video_pipeline.OperationBatch(10, 100)
    assert (batch.extraction_target, batch.capacity) == (4, 6)
    batch.waiting = 10
    batch.extractions = 4
    batch.check_supply()
    batch.ready = 1
    batch.check_supply()
    batch.ready = 0
    batch.check_supply()
    assert batch.extraction_target == 4
    batch.check_supply()
    assert batch.extraction_target == 5
    assert batch.capacity == 6


@pytest.mark.asyncio
async def test_retry_reuses_valid_frames_before_retrying_unprepared_items(tmp_path, monkeypatch, limits):
    limits()
    existing = tmp_path / "ready.jpg"
    existing.write_bytes(b"frozen")
    fresh = tmp_path / "fresh.jpg"
    fresh.write_bytes(b"new")
    prepared = []
    inputs = []
    task = Task(id="mixed-retry", mode="operation", options={"concurrency": 1}, items=[
        {"id": "new", "query": "q", "video_path": "ignored.mp4"},
        {"id": "ready", "query": "q", "frames": [str(existing)]},
    ])

    def prepare(item, **kwargs):
        prepared.append(item["id"])
        return {**item, "frames": [str(fresh)]}

    async def evaluate(*args, **kwargs):
        item = args[2]
        inputs.append((item["id"], Path(item["frames"][0]).read_bytes()))
        return {"item_id": item["id"], "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    await asyncio.wait_for(runner._run(task, load_config(Path("config")), rerun={"attempt_id": "retry"}), 3)
    assert inputs == [("ready", b"frozen"), ("new", b"new")]
    assert prepared == ["new"]
    assert [row["item_id"] for row in task.results] == ["new", "ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "replaced"])
async def test_full_retry_creates_new_generation_if_frozen_frames_are_corrupt(tmp_path, monkeypatch, limits, damage):
    limits()
    from auto_eval.web.operation_runs import freeze_frame_item

    video = tmp_path / "source.mp4"
    video.write_bytes(b"source video")
    calls = []

    def extract(source, frame_dir):
        assert source.read_bytes() == b"source video"
        calls.append(frame_dir)
        frame = frame_dir / "kf_001.jpg"
        frame.write_bytes(b"right image")
        return [frame]

    def prepare(item, **kwargs):
        kwargs.setdefault("runs_dir", tmp_path)
        return operation_media.prepare_session_operation_item(
            item, base_dir=tmp_path, probe_fn=lambda _: 5, extract_fn=extract, **kwargs,
        )

    task = Task(id="original", mode="operation", options={"execution_mode": "full"}, items=[])
    item = prepare({"id": "case", "query": "q", "video_path": str(video)},
                   session_name=task.session_name, item_index=0, total_items=1)
    old_cache = Path(item["frames"][0])
    freeze_frame_item(item, tmp_path, generation_id="original")
    old_frame_set = item["frame_set_id"]
    old_frame = Path(item["frames"][0])
    if damage == "missing":
        old_frame.unlink()
    else:
        old_frame.write_bytes(b"corrupted image")
    old_cache.write_bytes(b"stale image")
    task.items = [item]

    async def evaluate(*args, **kwargs):
        assert args[2]["frame_set_id"] != old_frame_set
        assert Path(args[2]["frames"][0]).read_bytes() == b"right image"
        return {"item_id": "case", "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    await runner._run(task, load_config(Path("config")), rerun={"attempt_id": "retry1"})
    assert len(calls) == 2 and calls[0] != calls[1]
    assert not calls[1].exists()
    assert task.results[0]["correctness"] == "ok"
    assert old_cache.read_bytes() == b"stale image"
    assert (old_frame.read_bytes() if old_frame.exists() else None) == (
        None if damage == "missing" else b"corrupted image"
    )


@pytest.mark.asyncio
async def test_failed_group_recovery_keeps_reference_and_other_group_evaluates(tmp_path, monkeypatch, limits):
    limits()
    from auto_eval.web.operation_runs import freeze_frame_item

    good = {"id": "good", "query": "q"}
    broken = {"id": "broken", "query": "q", "video_path": str(tmp_path / "missing.mp4")}
    for row in (good, broken):
        directory = tmp_path / row["id"]
        directory.mkdir()
        frame = directory / "kf_001.jpg"
        frame.write_bytes(row["id"].encode())
        row["frames"] = [str(frame)]
        freeze_frame_item(row, tmp_path)
    Path(broken["frames"][0]).unlink()
    original = broken["frame_set_id"], broken["frames"][:]
    task = Task(id="group-retry", mode="operation", options={"execution_mode": "full"}, items=[{
        "id": "case", "query": "q", "group_variants": [
            {"group_id": row["id"], "item": row} for row in (good, broken)
        ],
    }])
    evaluations = []

    async def evaluate(*args, **kwargs):
        rows = [group["item"] for group in args[2]["group_variants"]]
        evaluations.append([row["frame_set_id"] for row in rows if not row.get("prepare_error")])
        return {"item_id": "case", "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    await runner._run(task, load_config(Path("config")), rerun={"attempt_id": "retry"})
    assert evaluations == [[good["frame_set_id"]]]
    assert broken["prepare_error"]
    assert (broken["frame_set_id"], broken["frames"]) == original
    assert task.results[0]["frame_sets"] == [good["frame_set_id"]]
    assert not list(tmp_path.glob("frame-retry-*"))


@pytest.mark.asyncio
async def test_cancelled_recovery_keeps_original_reference_and_worker_scratch(tmp_path, monkeypatch, limits):
    limits()
    from auto_eval.web.operation_runs import freeze_frame_item

    source = tmp_path / "kf_001.jpg"
    source.write_bytes(b"before")
    item = {"id": "case", "query": "q", "video_path": "unused.mp4", "frames": [str(source)]}
    freeze_frame_item(item, tmp_path)
    Path(item["frames"][0]).unlink()
    original = item["frame_set_id"], item["frames"][:]
    task = Task(id="cancel-retry", mode="operation", items=[item], options={"execution_mode": "full"})
    scratch = []
    finish = threading.Event()

    def prepare(row, *, rebuild_dir, **kwargs):
        scratch.append(rebuild_dir)
        assert finish.wait(5)
        frame = rebuild_dir / "kf_001.jpg"
        frame.write_bytes(b"after")
        return {**row, "frames": [str(frame)]}

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    recovery = asyncio.create_task(runner._run(task, load_config(Path("config")), rerun={"attempt_id": "retry"}))
    try:
        await until(lambda: bool(scratch))
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
        assert scratch[0].is_dir()
        assert video_pipeline._COUNTS["extraction"] == 1
    finally:
        finish.set()
    await until(lambda: video_pipeline._COUNTS.get("extraction", 0) == 0)
    assert not scratch[0].exists()
    assert (item["frame_set_id"], item["frames"]) == original


@pytest.mark.asyncio
async def test_import_shares_slot_and_keeps_remote_video_when_leading_request_cancels(tmp_path, monkeypatch, limits):
    limits(extraction_concurrency=2)
    content = b"first remote object"
    calls = []
    started = threading.Event()
    finish = threading.Event()

    def download(_, directory, *, record_digest):
        path = directory / "video.mp4"
        path.write_bytes(content)
        record_digest(hashlib.sha256(content).hexdigest())
        return path

    def extract(path, directory):
        calls.append(path)
        started.set()
        assert finish.wait(5)
        jpg = directory / "kf_001.jpg"
        jpg.write_bytes(path.read_bytes())
        return [jpg]

    monkeypatch.setattr(remote_video, "download_remote_video", download)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(server, "probe_duration", lambda _: 5)
    monkeypatch.setattr(server, "extract_scene_keyframes", extract)
    url = "https://example.test/video.mp4"

    async def prepare(identity):
        return await server.api_prepare_operation(server.OperationPrepareReq(items=[
            {"id": identity, "query": identity, "video_path": url},
        ]))

    leader = asyncio.create_task(prepare("leader"))
    await until(started.is_set)
    follower = asyncio.create_task(prepare("follower"))
    try:
        await until(lambda: any(work.waiters == 2 for work in video_pipeline._IMPORT_EXTRACTIONS.values()))
        assert video_pipeline._COUNTS["extraction"] == 1
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert calls[0].is_file()
    finally:
        finish.set()
    reply = (await follower)["items"][0]
    assert (reply["id"], reply["query"], reply["video_path"]) == ("follower", "follower", url)
    assert len(calls) == 1 and not calls[0].exists()
    cached = (await prepare("cached"))["items"][0]
    assert cached["frames"] == reply["frames"] and len(calls) == 1
    content = b"changed remote object"
    changed = (await prepare("changed"))["items"][0]
    assert changed["frames"] != reply["frames"]
    assert Path(changed["frames"][0]).read_bytes() == content and len(calls) == 2


@pytest.mark.asyncio
async def test_import_cancels_last_queued_waiter_and_can_retry_after_failure(tmp_path, limits):
    limits(extraction_concurrency=1)
    calls = 0

    def extract():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("抽帧失败")
        return [tmp_path / "frame.jpg"]

    async def prepare():
        return await video_pipeline.extract_import_frames(tmp_path, list, extract)

    async with video_pipeline.stage_slot("extraction"):
        queued = [asyncio.create_task(prepare()) for _ in range(2)]
        await until(lambda: any(work.waiters == 2 for work in video_pipeline._IMPORT_EXTRACTIONS.values()))
        await until(lambda: bool(video_pipeline._EXTRACTION_QUEUE.groups))
        for task in queued:
            task.cancel()
        await asyncio.gather(*queued, return_exceptions=True)
        await until(lambda: not video_pipeline._EXTRACTION_QUEUE.groups)
    assert calls == 0
    with pytest.raises(ValueError, match="抽帧失败"):
        await prepare()
    assert await prepare() == [tmp_path / "frame.jpg"]
