import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from auto_eval import media
from auto_eval.config import load_config
from auto_eval.paths import PROJECT_ROOT
from auto_eval.web import runner
from auto_eval.web.operation_media import prepare_session_operation_item, _rich_content_timing
from auto_eval.web.tasks import Task
from auto_eval.web.operation_runs import freeze_frames, verify_frames


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    try:
        ffmpeg = media.resolve_ffmpeg_executable()
    except media.FFmpegUnavailableError:
        pytest.skip("requires FFmpeg")
    root = tmp_path_factory.mktemp("web_front_protect")
    path = root / "card.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=0xeeeeee:s=240x400:r=10:d=36", "-vf",
        "drawbox=x=40:y=140:w=160:h=70:color=0xcecece:t=fill:enable='gte(t,9.7)*lt(t,10.7)'",
        "-c:v", "mpeg4", "-q:v", "2", str(path)], check=True, capture_output=True)
    return path


def metadata(directory):
    return json.loads((directory / "keyframes.json").read_text("utf-8"))


def test_default_extractor_keeps_local_card_without_changing_encoding(recording, tmp_path):
    config = media.KeyframeConfig(task_start_time=7, task_end_time=34)
    old = media.extract_scene_keyframes(recording, tmp_path / "old", config=config, front_protection=False)
    new = media.extract_scene_keyframes(recording, tmp_path / "new", config=config)
    a, b = metadata(tmp_path / "old"), metadata(tmp_path / "new")
    assert [row["time"] for row in a["selected"]] == [7, 34]
    assert [row["time"] for row in b["selected"]] == [7, 10, 34]
    assert b["selected"][1]["source"] == "protected-1s"
    assert a["config"] == b["config"]
    assert a["algorithm_version"] == b["algorithm_version"] == "hybrid-state-v3.2.1"
    assert old[0].read_bytes() == new[0].read_bytes() and old[-1].read_bytes() == new[-1].read_bytes()


def test_same_sampling_images_only_first_ten_seconds_get_new_protection(recording, tmp_path):
    config = media.KeyframeConfig(task_start_time=7, task_end_time=34)
    _, old = media._extract_candidates(recording, tmp_path / "old", config, 36, front_protection=False)
    _, new = media._extract_candidates(recording, tmp_path / "new", config, 36)
    assert len(old) == len(new)
    changed = []
    for a, b in zip(old, new):
        assert a.time == b.time and a.path.name == b.path.name
        assert hashlib.sha256(a.path.read_bytes()).digest() == hashlib.sha256(b.path.read_bytes()).digest()
        if a.source != b.source:
            assert a.source == "1fps" and b.source == "protected-1s"
            assert 7 <= b.time <= 17
            changed.append(b.time)
    assert changed == [8, 10, 12, 14, 16]


def test_current_version_required_only_for_operation_frames(tmp_path):
    frame = tmp_path / "kf_001.jpg"
    frame.write_bytes(b"original evidence")
    item = {"frames": [str(frame)]}
    assert runner._valid_cached_frames(item)
    assert not runner._valid_cached_frames(item, operation=True)
    (tmp_path / "keyframes.json").write_text(json.dumps({"algorithm_version": "hybrid-state-v3.2.0"}), "utf-8")
    assert not runner._valid_cached_frames(item, operation=True)
    (tmp_path / "keyframes.json").write_text(json.dumps({"algorithm_version": media.KEYFRAME_ALGORITHM_VERSION}), "utf-8")
    assert runner._valid_cached_frames(item, operation=True)


def test_session_preparation_does_not_overwrite_old_history(recording, tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(recording.parent))
    previous = tmp_path / "runs/videos/imported/session/001_case"
    previous.mkdir(parents=True)
    (previous / "kf_001.jpg").write_bytes(b"keep historical evidence")
    item = {"id": "case", "query": "q", "video_path": str(recording), "task_start_time": 7, "task_end_time": 34}
    prepared = prepare_session_operation_item(item, session_name="session", item_index=0, total_items=1, runs_dir=tmp_path / "runs")
    assert Path(prepared["frames"][0]).parent != previous
    assert (previous / "kf_001.jpg").read_bytes() == b"keep historical evidence"
    assert [r["time"] for r in metadata(Path(prepared["frames"][0]).parent)["selected"]] == [7, 10, 34]


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True])
async def test_normal_web_runner_reextracts_old_frames_and_submits_new_card(recording, tmp_path, monkeypatch, multi):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(recording.parent))
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = old_dir / "kf_001.jpg"
    old.write_bytes(b"keep old frame")
    (old_dir / "keyframes.json").write_text(json.dumps({"algorithm_version": "hybrid-state-v3.2.0"}), "utf-8")
    def item():
        return {"id": "case", "query": "q", "video_path": str(recording), "frames": [str(old)],
                "task_start_time": 7, "task_end_time": 34}
    case = item()
    if multi:
        case.update(evaluation_strategy="multi_group", group_variants=[
            {"group_id": "a", "group_name": "A", "group_role": "experiment", "item": item()}])
    task = Task(id="web-front", mode="operation", items=[case], options={"concurrency": 1})
    prepare = runner.prepare_session_operation_item
    monkeypatch.setattr(runner, "prepare_session_operation_item", lambda i, **kwargs: prepare(i, runs_dir=tmp_path / "runs", **kwargs))
    captured = []
    async def evaluate(mode, index, current, *args, **kwargs):
        actual = current["group_variants"][0]["item"] if multi else current
        captured.append(actual["frames"])
        assert [r["time"] for r in metadata(Path(actual["frames"][0]).parent)["selected"]] == [7, 10, 34]
        return {"correctness": "ok", "total": None}
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    await runner._run(task, load_config(PROJECT_ROOT / "config"))
    assert captured and task.results[0]["correctness"] == "ok"
    assert old.read_bytes() == b"keep old frame"


def test_non_operation_extraction_keeps_its_original_rules():
    profile = load_config(PROJECT_ROOT / "config").visual_modes["rich_content"]
    kwargs, _ = _rich_content_timing({}, 30, profile)
    assert kwargs["front_protection"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("execution_mode", ["full", "extract_only", "evaluate_only"])
async def test_execution_modes_handle_old_frozen_frames(recording, tmp_path, monkeypatch, multi, execution_mode):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(recording.parent))
    runs = tmp_path / "runs"
    old_dir = runs / "old"
    old_dir.mkdir(parents=True)
    old_frame = old_dir / "kf_001.jpg"
    old_frame.write_bytes(b"old evidence")
    (old_dir / "keyframes.json").write_text(json.dumps({
        "algorithm_version": "hybrid-state-v3.2.0",
        "selected": [{"index": 1, "time": 7}],
    }), encoding="utf-8")
    row = {"id": "case", "query": "q", "video_path": str(recording),
           "frames": [str(old_frame)], "task_start_time": 7, "task_end_time": 34}
    item = {"id": "case", "query": "q", "group_variants": [
        {"group_id": "a", "group_name": "A", "group_role": "experiment", "item": row},
    ]} if multi else row
    freeze_frames(item, runs_dir=runs)
    old_identity = row["frame_set_id"]
    old_archive = Path(row["frames"][0]).parent
    original_files = {path.name: path.read_bytes() for path in old_archive.iterdir()}
    task = Task(id="mode-front", mode="operation", items=[item], options={"execution_mode": execution_mode})
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: True)
    prepare = runner.prepare_session_operation_item
    prepared = []

    def prepare_current(current, **kwargs):
        assert execution_mode != "evaluate_only"
        assert "frame_set_id" not in current and "frame_manifest_path" not in current
        prepared.append(True)
        return prepare(current, runs_dir=runs, **kwargs)

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare_current)
    evaluated = []

    async def evaluate(mode, index, current, *args, **kwargs):
        evaluated.append(True)
        actual = current["group_variants"][0]["item"] if multi else current
        expected = "hybrid-state-v3.2.0" if execution_mode == "evaluate_only" else media.KEYFRAME_ALGORITHM_VERSION
        assert metadata(Path(actual["frames"][0]).parent)["algorithm_version"] == expected
        return {"correctness": "ok", "category": "operation"}

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    if execution_mode == "evaluate_only":
        runner._prepare_rerun_items(task, [0])
        assert row["frame_set_id"] == old_identity
    await runner._run(task, load_config(PROJECT_ROOT / "config"))
    assert not task.results[0].get("error"), task.results
    assert bool(prepared) == (execution_mode != "evaluate_only")
    assert bool(evaluated) == (execution_mode != "extract_only")
    assert (row["frame_set_id"] == old_identity) == (execution_mode == "evaluate_only")
    verify_frames(item, runs)
    assert {path.name: path.read_bytes() for path in old_archive.iterdir()} == original_files
    if execution_mode != "evaluate_only":
        assert [r["time"] for r in metadata(Path(row["frames"][0]).parent)["selected"]] == [7, 10, 34]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["operation", "rich_content_quality"])
async def test_upload_preserves_front_protection_with_staged_preparation(tmp_path, monkeypatch, mode):
    from io import BytesIO
    from fastapi import UploadFile
    from auto_eval.web import server

    monkeypatch.setattr(server, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(server, "probe_duration", lambda _: 12)
    calls = []

    def extract(video, directory, **kwargs):
        calls.append(kwargs.get("front_protection", True))
        return []

    monkeypatch.setattr(server, "extract_scene_keyframes", extract)
    result = await server.api_upload_video(UploadFile(filename="test.mp4", file=BytesIO(b"video")), mode=mode)
    assert calls == [mode == "operation"]
    assert result["duration"] == 12
