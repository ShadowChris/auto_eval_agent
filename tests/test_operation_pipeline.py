from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from auto_eval.config import load_config
from auto_eval.web import operation_media, pipeline_config, remote_video, runner, video_pipeline
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
            await video_pipeline.prepare_video(extract, {"video_path": "https://cdn.test/a"})

    first = asyncio.create_task(item())
    try:
        await until(entered.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert downloads[0].is_file()
        assert video_pipeline._COUNTS["extraction"] == 1
        second = asyncio.create_task(video_pipeline.prepare_video(lambda: second_entered.set()))
        await asyncio.sleep(0.06)
        assert not second_entered.is_set()
    finally:
        finish.set()
    await second
    await until(lambda: not downloads[0].exists())
    assert video_pipeline._COUNTS["extraction"] == 0


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
        (directory / "keyframes.json").write_text(json.dumps({
            "algorithm_version": operation_media.KEYFRAME_ALGORITHM_VERSION,
        }), encoding="utf-8")
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
