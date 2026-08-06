import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from auto_eval.media import (
    FFmpegUnavailableError,
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    _Candidate,
    _final_deduplicate,
    extract_scene_keyframes,
    probe_duration,
    resolve_ffmpeg_executable,
)


def _ffmpeg_available() -> bool:
    try:
        resolve_ffmpeg_executable()
        return True
    except FFmpegUnavailableError:
        return False


FFMPEG_AVAILABLE = _ffmpeg_available()


def _write_image(path: Path, value: int) -> Path:
    image = Image.new("L", (240, 400), value)
    image.save(path, format="JPEG", quality=95)
    return path


def test_final_deduplicate_keeps_protected_frames_and_drops_strict_duplicates(
    tmp_path: Path,
):
    first = _Candidate(
        7.0,
        _write_image(tmp_path / "first.jpg", 245),
        "start-7.0s",
        keep_reason="stable-state-start",
    )
    duplicate_stable = _Candidate(
        10.0,
        _write_image(tmp_path / "duplicate_stable.jpg", 245),
        "1fps",
        keep_reason="stable-state-end",
    )
    task_end = _Candidate(
        20.0,
        _write_image(tmp_path / "task_end.jpg", 245),
        "1fps",
        keep_reason="task-end-auto",
    )
    duplicate_before_final = _Candidate(
        25.0,
        _write_image(tmp_path / "duplicate_before_final.jpg", 100),
        "1fps",
        keep_reason="stable-state-end",
    )
    final = _Candidate(
        30.0,
        _write_image(tmp_path / "final.jpg", 100),
        "terminal-0.3s",
        keep_reason="final-frame",
    )

    kept, removed = _final_deduplicate(
        [first, duplicate_stable, task_end, duplicate_before_final, final],
        KeyframeConfig(),
    )

    assert kept == [first, task_end, final]
    assert removed == [duplicate_stable, duplicate_before_final]


def test_keyframe_config_rejects_invalid_sampling_values():
    with pytest.raises(ValueError, match="sample_fps"):
        KeyframeConfig(sample_fps=0)
    with pytest.raises(ValueError, match="max_frames"):
        KeyframeConfig(max_frames=1)


def test_keyframe_algorithm_version_is_frozen_baseline():
    assert KEYFRAME_ALGORITHM_VERSION == "hybrid-state-v3.1.0"


def test_keyframe_config_uses_expanded_protected_window_and_frame_limit():
    config = KeyframeConfig()

    assert config.protected_begin_window == 20.0
    assert config.max_frames == 20


def test_keyframe_config_uses_unified_task_time_names():
    config = KeyframeConfig(task_start_time=6.0, task_end_time=12.0)

    assert config.task_start_time == 6.0
    assert config.task_end_time == 12.0
    with pytest.raises(ValueError, match="task_end_time 必须大于"):
        KeyframeConfig(task_start_time=8.0, task_end_time=8.0)


def test_resolve_ffmpeg_uses_explicit_path(tmp_path: Path, monkeypatch):
    executable = tmp_path / "custom-ffmpeg"
    executable.write_bytes(b"binary")
    monkeypatch.setenv("AUTO_EVAL_FFMPEG", str(executable))

    assert resolve_ffmpeg_executable() == str(executable.resolve())


def test_resolve_ffmpeg_rejects_missing_explicit_path(monkeypatch):
    monkeypatch.setenv("AUTO_EVAL_FFMPEG", "/missing/custom-ffmpeg")

    with pytest.raises(FFmpegUnavailableError, match="AUTO_EVAL_FFMPEG"):
        resolve_ffmpeg_executable()


def test_resolve_ffmpeg_falls_back_to_pip_wheel(tmp_path: Path, monkeypatch):
    bundled = tmp_path / "bundled-ffmpeg"
    bundled.write_bytes(b"binary")
    monkeypatch.delenv("AUTO_EVAL_FFMPEG", raising=False)
    monkeypatch.setattr("auto_eval.media.shutil.which", lambda name: None)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: str(bundled)),
    )

    assert resolve_ffmpeg_executable() == str(bundled.resolve())


def test_probe_duration_falls_back_to_ffmpeg_metadata(monkeypatch):
    monkeypatch.setattr("auto_eval.media._resolve_ffprobe_executable", lambda: None)
    monkeypatch.setattr("auto_eval.media.resolve_ffmpeg_executable", lambda: "bundled-ffmpeg")
    monkeypatch.setattr(
        "auto_eval.media.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stderr="Duration: 01:02:03.45, start: 0.000000, bitrate: 800 kb/s"
        ),
    )

    assert probe_duration("sample.mp4") == pytest.approx(3723.45)


@pytest.mark.skipif(
    not FFMPEG_AVAILABLE,
    reason="requires system ffmpeg or the video extra",
)
def test_extract_scene_keyframes_preserves_popup_task_end_and_final_frame(
    tmp_path: Path,
):
    video = tmp_path / "popup_flow.mp4"
    subprocess.run(
        [
            resolve_ffmpeg_executable(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=240x400:r=10:d=16",
            "-vf",
            (
                "drawbox=x=30:y=110:w=180:h=120:color=black:t=fill:"
                "enable='gte(t,8)*lt(t,10)',"
                "drawbox=x=0:y=0:w=240:h=400:color=gray:t=fill:"
                "enable='gte(t,14)'"
            ),
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            str(video),
        ],
        check=True,
    )

    out_dir = tmp_path / "frames"
    frames = extract_scene_keyframes(
        video,
        out_dir,
        config=KeyframeConfig(
            task_start_time=7.0,
            task_end_time=13.0,
            max_frames=10,
            max_edge=240,
        ),
    )
    metadata = json.loads(
        (out_dir / "keyframes.json").read_text(encoding="utf-8")
    )

    reasons = [row["keep_reason"] for row in metadata["selected"]]
    assert metadata["effective_task_end_time"] == 13.0
    assert metadata["selected"][0]["time"] == 7.0
    assert "task-end-explicit" in reasons
    assert reasons[-1] == "final-frame"
    assert 3 <= len(frames) <= 6

    popup_indices = [
        index
        for index, row in enumerate(metadata["selected"])
        if 8.0 <= row["time"] <= 10.0
    ]
    assert popup_indices
    assert any(
        float(np.mean(np.asarray(Image.open(frames[index]).convert("L")) < 40))
        > 0.15
        for index in popup_indices
    )

    final_mean = float(np.mean(np.asarray(Image.open(frames[-1]).convert("L"))))
    assert 90 < final_mean < 180


@pytest.mark.skipif(
    not FFMPEG_AVAILABLE,
    reason="requires system ffmpeg or the video extra",
)
def test_expanded_begin_window_preserves_late_local_popup(tmp_path: Path):
    video = tmp_path / "late_popup.mp4"
    subprocess.run(
        [
            resolve_ffmpeg_executable(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=240x400:r=10:d=30",
            "-vf",
            "drawbox=x=20:y=20:w=200:h=100:color=black:t=fill:"
            "enable='gte(t,18)*lt(t,20)'",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            str(video),
        ],
        check=True,
    )

    out_dir = tmp_path / "frames"
    frames = extract_scene_keyframes(
        video,
        out_dir,
        config=KeyframeConfig(
            task_start_time=7.0,
            task_end_time=27.0,
            scene_threshold=1.0,
            state_layout_threshold=1.0,
            max_edge=240,
        ),
    )
    metadata = json.loads(
        (out_dir / "keyframes.json").read_text(encoding="utf-8")
    )

    popup_indices = [
        index
        for index, row in enumerate(metadata["selected"])
        if 18.0 <= row["time"] <= 20.0
    ]
    assert popup_indices
    assert any(
        float(np.mean(np.asarray(Image.open(frames[index]).convert("L")) < 40))
        > 0.15
        for index in popup_indices
    )
