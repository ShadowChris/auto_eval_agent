from dataclasses import asdict
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest
import yaml

from auto_eval import media, operation_keyframes
from auto_eval.config import load_config
from auto_eval.paths import PROJECT_ROOT
from auto_eval.web import operation_versions, server
from auto_eval.web.operation_media import _operation_timing, prepare_cached_operation_item
from auto_eval.web.operation_runs import evaluation_snapshot, frame_algorithm_version
from auto_eval.web.operation_versions import (
    DEFAULT_KEYFRAME_VERSION,
    DEFAULT_STANDARD_VERSION,
    LEGACY_KEYFRAME_VERSION,
    get_keyframe_version,
    pin_operation_versions,
    standard_snapshot,
)


@pytest.fixture(autouse=True)
def isolated_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_versions, "DEFAULTS_PATH", tmp_path / "settings" / "versions.json")


@pytest.fixture
def bundles(tmp_path, monkeypatch):
    root = tmp_path / "bundles"
    shutil.copytree(operation_versions.BUNDLE_ROOT, root)
    monkeypatch.setattr(operation_versions, "BUNDLE_ROOT", root)
    operation_versions._manifests.cache_clear()
    operation_versions._keyframe_version.cache_clear()
    yield root / "keyframes"
    operation_versions._manifests.cache_clear()
    operation_versions._keyframe_version.cache_clear()


def test_baseline_preserves_original_prompt_policy_and_weight_snapshot():
    expected = evaluation_snapshot(load_config(PROJECT_ROOT / "config"))
    assert standard_snapshot(DEFAULT_STANDARD_VERSION) == expected
    snapshot = standard_snapshot(DEFAULT_STANDARD_VERSION)
    snapshot["templates"].clear()
    assert standard_snapshot(DEFAULT_STANDARD_VERSION) == expected


def test_web_catalog_has_both_real_extractors_and_structured_standard():
    client = TestClient(server.app)
    catalog = client.get("/api/operation/versions").json()
    assert catalog["defaults"] == {
        "keyframe_version": DEFAULT_KEYFRAME_VERSION, "standard_version": DEFAULT_STANDARD_VERSION,
    }
    assert {entry["id"]: entry["updated_at"] for entry in catalog["keyframes"]} == {
        DEFAULT_KEYFRAME_VERSION: "2026-11-19",
        LEGACY_KEYFRAME_VERSION: "2026-09-29",
    }
    assert {entry["id"]: entry["updated_at"] for entry in catalog["standards"]} == {
        DEFAULT_STANDARD_VERSION: "2026-11-25",
    }
    assert {entry["id"]: entry["name"] for entry in catalog["keyframes"]} == {
        DEFAULT_KEYFRAME_VERSION: "v4.13", LEGACY_KEYFRAME_VERSION: "v3.2.0",
    }
    assert catalog["standards"][0]["name"] == "v1.0"
    assert get_keyframe_version(LEGACY_KEYFRAME_VERSION).extract.__module__ == "auto_eval.media"
    assert get_keyframe_version(DEFAULT_KEYFRAME_VERSION).extract.__module__ == "auto_eval.operation_keyframes"
    response = client.get(f"/api/operation/versions/standards/{DEFAULT_STANDARD_VERSION}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["updated_at"] == "2026-11-25"
    assert set(detail["policy"]["correctness"]) == {"ok", "nok", "no_support", "others"}
    assert detail["policy"]["issue_types"] and detail["rubrics"] and detail["expert_knowledge"]
    assert detail["policy_hash"] == standard_snapshot(DEFAULT_STANDARD_VERSION)["policy_hash"]


def test_defaults_are_persisted_and_dont_change_existing_task_options():
    client = TestClient(server.app)
    first = pin_operation_versions({"execution_mode": "full", "evaluation_snapshot": {"fake": True}})
    payload = {"keyframe_version": LEGACY_KEYFRAME_VERSION, "standard_version": DEFAULT_STANDARD_VERSION}
    response = client.put("/api/operation/versions/defaults", json=payload)
    assert response.status_code == 200
    assert client.get("/api/operation/versions").json()["defaults"] == payload
    assert operation_versions.DEFAULTS_PATH.is_file()
    assert first["keyframe_version"] == DEFAULT_KEYFRAME_VERSION
    assert "fake" not in first["evaluation_snapshot"]
    assert pin_operation_versions({})["keyframe_version"] == LEGACY_KEYFRAME_VERSION
    assert pin_operation_versions({"keyframe_version": DEFAULT_KEYFRAME_VERSION})["keyframe_version"] == DEFAULT_KEYFRAME_VERSION
    assert client.put("/api/operation/versions/defaults", json={**payload, "standard_version": "missing"}).status_code == 422
    assert client.get("/api/operation/versions").json()["defaults"] == payload


def test_stage_modes_pin_only_versions_they_use():
    prepare = pin_operation_versions({"execution_mode": "extract_only", "standard_version": DEFAULT_STANDARD_VERSION})
    assert prepare["keyframe_version"] == DEFAULT_KEYFRAME_VERSION
    assert "standard_version" not in prepare and "evaluation_snapshot" not in prepare
    evaluate = pin_operation_versions({"execution_mode": "evaluate_only"})
    assert "keyframe_version" not in evaluate and evaluate["standard_version"] == DEFAULT_STANDARD_VERSION
    with pytest.raises(ValueError, match="沿用实际归档帧"):
        pin_operation_versions({"execution_mode": "evaluate_only", "keyframe_version": DEFAULT_KEYFRAME_VERSION})
    with pytest.raises(ValueError, match="未知抽帧版本"):
        pin_operation_versions({"keyframe_version": "../arbitrary"})


@pytest.mark.asyncio
async def test_algorithm_versions_use_separate_caches(tmp_path):
    video = tmp_path / "source.mp4"
    video.touch()
    seen = []

    def extract(_video, directory, **kwargs):
        seen.append((directory, kwargs))
        path = directory / "kf_001.jpg"
        path.touch()
        return [path]

    item = {"query": "q", "video_path": str(video), "task_start_time": 0}
    async def prepare(version: str) -> dict:
        return await prepare_cached_operation_item(
            item, base_dir=tmp_path, runs_dir=tmp_path / "runs",
            keyframe_version=version, probe_fn=lambda _: 2, extract_fn=extract,
        )

    legacy = await prepare(LEGACY_KEYFRAME_VERSION)
    current = await prepare(DEFAULT_KEYFRAME_VERSION)
    assert legacy["frames"] == (await prepare(LEGACY_KEYFRAME_VERSION))["frames"]
    assert legacy["frames"] != current["frames"]
    assert len(seen) == 2
    assert frame_algorithm_version(legacy, tmp_path / "runs") is None


def test_previous_and_current_cache_encode_actual_default_config():
    current = get_keyframe_version(DEFAULT_KEYFRAME_VERSION)
    legacy = get_keyframe_version(LEGACY_KEYFRAME_VERSION)
    assert current.config().sample_fps == 3 and current.config().max_frames == 15
    assert legacy.config().sample_fps == 1 and legacy.config().max_frames == 20
    assert legacy.config() == media.KeyframeConfig()
    assert current.config() == operation_keyframes.KeyframeConfig()
    assert _operation_timing({}, 20, legacy)[1] != _operation_timing({}, 20, current)[1]


@pytest.mark.parametrize("change,message", [("edit", "参数校验失败"), ("delete", "保存全部")])
def test_released_params_cannot_silently_drift_or_fall_back_to_python_defaults(bundles, change, message):
    path = bundles / "v4.13" / "params.yaml"
    params = yaml.safe_load(path.read_text())
    if change == "edit":
        params["sample_fps"] = 2.0
    else:
        params.pop("sample_fps")
    path.write_text(yaml.safe_dump(params))
    with pytest.raises(ValueError, match=message):
        get_keyframe_version(DEFAULT_KEYFRAME_VERSION)


@pytest.mark.asyncio
async def test_new_params_only_version_reuses_engine_and_drives_cache_and_upload(bundles, tmp_path, monkeypatch):
    version = "tuned-test"
    folder = bundles / version
    folder.mkdir()
    params = yaml.safe_load((bundles / "v4.13" / "params.yaml").read_text())
    params.update(sample_fps=1.5, max_frames=8)
    (folder / "params.yaml").write_text(yaml.safe_dump(params))
    (folder / "manifest.yaml").write_text(yaml.safe_dump({
        "id": version, "name": "Tuned", "engine": DEFAULT_KEYFRAME_VERSION,
        "previous": DEFAULT_KEYFRAME_VERSION, "changes": ["lower sampling"],
        "updated_at": "2026-11-26",
        "params_hash": operation_versions._content_digest(params),
    }))

    observed = []
    def extract(_video, directory, *, config, algorithm_version):
        observed.append((algorithm_version, config))
        directory.mkdir(parents=True, exist_ok=True)
        frame = directory / "kf_001.jpg"
        frame.touch()
        return [frame]

    monkeypatch.setitem(operation_versions.KEYFRAME_ENGINES, DEFAULT_KEYFRAME_VERSION,
                        (extract, operation_keyframes.KeyframeConfig))
    runs = tmp_path / "runs"
    video = tmp_path / "source.mp4"
    video.touch()
    row = await prepare_cached_operation_item(
        {"query": "q", "video_path": str(video), "task_start_time": 0, "task_end_time": 2.0},
        base_dir=tmp_path, runs_dir=runs, keyframe_version=version, probe_fn=lambda _: 5,
    )
    assert observed[0][0] == version
    effective = observed[0][1]
    assert (effective.sample_fps, effective.max_frames, effective.task_start_time, effective.task_end_time) == (1.5, 8, 0, 2)
    marker = Path(row["frames"][0]).parent / ".complete"
    cache = json.loads(json.loads(marker.read_text())["cache_key"])
    assert cache["config"] == json.loads(json.dumps(asdict(effective)))

    monkeypatch.setattr(server, "RUNS_DIR", runs)
    monkeypatch.setattr(server, "probe_duration", lambda _: 10)
    response = TestClient(server.app).post(
        "/api/upload/video", params={"mode": "operation", "keyframe_version": version},
        files={"file": ("video.mp4", b"mock video", "video/mp4")},
    )
    assert response.status_code == 200
    assert observed[1] == (version, get_keyframe_version(version).config())
