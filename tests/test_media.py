import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from auto_eval.media import (
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    _Candidate,
    _final_deduplicate,
    extract_scene_keyframes,
)


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
    assert KEYFRAME_ALGORITHM_VERSION == "hybrid-state-v3.0.0"


def test_keyframe_config_uses_unified_task_time_names():
    config = KeyframeConfig(task_start_time=6.0, task_end_time=12.0)

    assert config.task_start_time == 6.0
    assert config.task_end_time == 12.0
    with pytest.raises(ValueError, match="task_end_time 必须大于"):
        KeyframeConfig(task_start_time=8.0, task_end_time=8.0)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requires local ffmpeg and ffprobe",
)
def test_extract_scene_keyframes_preserves_popup_task_end_and_final_frame(
    tmp_path: Path,
):
    video = tmp_path / "popup_flow.mp4"
    subprocess.run(
        [
            "ffmpeg",
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
