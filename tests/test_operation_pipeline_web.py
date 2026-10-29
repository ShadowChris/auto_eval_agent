import asyncio
from dataclasses import asdict

from fastapi.testclient import TestClient
import pytest

from auto_eval.web import pipeline_config, server, video_pipeline
from auto_eval.web.pipeline_config import OperationPipelineConfig, PipelineConfigStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = PipelineConfigStore(tmp_path / "settings" / "operation_pipeline.json")
    monkeypatch.setattr(pipeline_config, "_SETTINGS", settings)
    return settings


async def eventually(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("调度未按预期更新")


def test_api_config_persists_and_survives_store_recreation(store):
    client = TestClient(server.app)
    assert client.get("/api/operation/pipeline-config").json() == asdict(OperationPipelineConfig())
    values = {"download_concurrency": 13, "extraction_concurrency": 4, "model_concurrency": 20}
    response = client.put("/api/operation/pipeline-config", json=values)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "config": values}
    assert client.get("/api/operation/pipeline-config").json() == values
    assert asdict(PipelineConfigStore(store.path).get()) == values
    assert not list(store.path.parent.glob("*.tmp"))


@pytest.mark.parametrize("field,value", [
    ("download_concurrency", 0), ("download_concurrency", 129),
    ("extraction_concurrency", -1), ("model_concurrency", 1.5),
    ("model_concurrency", True), ("model_concurrency", "5"),
])
def test_api_rejects_invalid_values_without_changing_live_config(store, field, value):
    client = TestClient(server.app)
    values = asdict(OperationPipelineConfig())
    values[field] = value
    response = client.put("/api/operation/pipeline-config", json=values)
    assert response.status_code == 422
    assert store.get() == OperationPipelineConfig()
    assert not store.path.exists()


def test_api_rejects_incomplete_or_unknown_settings(store):
    client = TestClient(server.app)
    assert client.put("/api/operation/pipeline-config", json={"download_concurrency": 3}).status_code == 422
    assert client.put("/api/operation/pipeline-config", json={
        **asdict(OperationPipelineConfig()), "extracton_concurrency": 2,
    }).status_code == 422


def test_failed_save_preserves_previous_disk_and_live_config(store, monkeypatch):
    original = asdict(OperationPipelineConfig(8, 2, 10))
    store.save(original)
    before = store.path.read_bytes()

    def fail(*_args):
        raise PermissionError("read only")

    monkeypatch.setattr(pipeline_config.os, "replace", fail)
    response = TestClient(server.app).put("/api/operation/pipeline-config", json=asdict(OperationPipelineConfig()))
    assert response.status_code == 500
    assert "写入权限" in response.json()["detail"]
    assert asdict(store.get()) == original
    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["download", "extraction", "model"])
async def test_increasing_limit_releases_already_queued_stages(store, stage):
    values = asdict(OperationPipelineConfig())
    key = f"{stage}_concurrency"
    values[key] = 1
    store.save(values)
    entered = [asyncio.Event(), asyncio.Event()]
    finish = asyncio.Event()

    async def worker(index):
        async with video_pipeline.stage_slot(stage):
            entered[index].set()
            await finish.wait()

    tasks = [asyncio.create_task(worker(index)) for index in range(2)]
    try:
        await eventually(entered[0].is_set)
        await asyncio.sleep(0.07)
        assert not entered[1].is_set()
        values[key] = 2
        store.save(values)
        await eventually(entered[1].is_set)
        assert video_pipeline._COUNTS[stage] == 2
    finally:
        finish.set()
        await asyncio.gather(*tasks)
    assert video_pipeline._COUNTS[stage] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["download", "extraction", "model"])
async def test_lowering_limit_waits_for_inflight_work_including_previous_unlimited(store, stage):
    values = asdict(OperationPipelineConfig())
    key = f"{stage}_concurrency"
    values[key] = 2 if stage == "download" else 0
    store.save(values)
    entered = [asyncio.Event() for _ in range(3)]
    finishes = [asyncio.Event() for _ in range(3)]

    async def worker(index):
        async with video_pipeline.stage_slot(stage):
            entered[index].set()
            await finishes[index].wait()

    tasks = [asyncio.create_task(worker(index)) for index in range(2)]
    try:
        await eventually(lambda: entered[0].is_set() and entered[1].is_set())
        values[key] = 1
        store.save(values)
        tasks.append(asyncio.create_task(worker(2)))
        await asyncio.sleep(0.07)
        assert not entered[2].is_set()
        finishes[0].set()
        await tasks[0]
        await asyncio.sleep(0.07)
        assert not entered[2].is_set()
        finishes[1].set()
        await eventually(entered[2].is_set)
        assert video_pipeline._COUNTS[stage] == 1
    finally:
        for finish in finishes:
            finish.set()
        await asyncio.gather(*tasks)
    assert video_pipeline._COUNTS[stage] == 0


def test_web_exposes_service_controls_and_does_not_restore_them_from_history():
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "应用并保存" in html
    assert 'aria-label="整体任务并发"' in html
    assert 'aria-label="视频下载并发"' in html
    assert "当前批次同时处理的题目上限" in html
    assert "影响所有正在运行和后续批跑" in html
    for key in ("download_concurrency", "extraction_concurrency", "model_concurrency"):
        assert f'v-model.number="pipelineConfig.{key}"' in html
    assert 'method: "PUT"' in script
    assert "if (d.operation_pipeline) applyPipelineSnapshot(d.operation_pipeline)" in script
    assert "applyPipelineSnapshot(options." not in script
