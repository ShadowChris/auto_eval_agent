"""视频视觉评估的路径校验、缓存与关键帧准备。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from numbers import Real
from pathlib import Path
from typing import Callable

from ..media import (
    DEFAULT_TASK_START_TIME,
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    extract_scene_keyframes,
    probe_duration,
)
from ..config import VisualModeProfile
from ..paths import PROJECT_ROOT, RUNS_DIR


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_TASK_TIME_FIELDS = ("task_start_time", "task_end_time")
_CONTENT_TIME_FIELDS = ("content_start_time", "content_end_time")


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", value.strip())
    return safe or fallback


def operation_video_roots(base_dir: Path = PROJECT_ROOT) -> list[Path]:
    """返回批量清单允许读取的视频根目录。"""
    roots = [base_dir.resolve()]
    for raw in os.getenv("OPERATION_VIDEO_ROOTS", "").split(os.pathsep):
        if raw.strip():
            root = Path(raw.strip()).expanduser()
            if not root.is_absolute():
                root = base_dir / root
            roots.append(root.resolve())
    return roots


def resolve_operation_video_path(
    raw_path: str,
    *,
    base_dir: Path = PROJECT_ROOT,
) -> Path:
    """解析本地视频路径，并阻止读取未授权目录。"""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    candidate = candidate.resolve()
    if not any(candidate.is_relative_to(root) for root in operation_video_roots(base_dir)):
        raise ValueError(
            "视频路径不在允许目录中；相对路径请以项目根目录为基准，"
            "外部目录需通过 OPERATION_VIDEO_ROOTS 配置"
        )
    if not candidate.is_file():
        raise ValueError(f"视频文件不存在：{raw_path}")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"不支持的视频格式 {candidate.suffix or '(无扩展名)'}；支持：{supported}")
    return candidate


def _cached_frames(
    frame_dir: Path,
    cache_key: str = KEYFRAME_ALGORITHM_VERSION,
) -> list[Path]:
    marker = frame_dir / ".complete"
    if not marker.exists():
        return []
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("cache_key") != cache_key:
            return []
        expected = int(payload["frame_count"])
        frames = sorted(frame_dir.glob("kf_*.jpg"))
        return frames if expected > 0 and len(frames) == expected else []
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        return []


def _extract_frames(
    video_path: Path,
    frame_dir: Path,
    *,
    extract_fn: Callable = extract_scene_keyframes,
    cache_key: str = KEYFRAME_ALGORITHM_VERSION,
    extract_kwargs: dict | None = None,
) -> list[Path]:
    frames = _cached_frames(frame_dir, cache_key)
    if frames:
        return frames
    frame_dir.mkdir(parents=True, exist_ok=True)
    for stale in frame_dir.glob("kf_*.jpg"):
        stale.unlink(missing_ok=True)
    (frame_dir / ".complete").unlink(missing_ok=True)
    (frame_dir / "keyframes.json").unlink(missing_ok=True)
    frames = list(extract_fn(video_path, frame_dir, **(extract_kwargs or {})))
    if frames:
        (frame_dir / ".complete").write_text(
            json.dumps(
                {
                    "cache_key": cache_key,
                    "frame_count": len(frames),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return frames


def _operation_timing(
    item: dict,
    duration: float,
) -> tuple[dict[str, float], str]:
    """校验任务起止时间，并生成抽帧参数和稳定缓存键。"""
    supplied: dict[str, float] = {}
    for field in _TASK_TIME_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        if normalized < 0:
            raise ValueError(f"{field} 不能小于 0")
        if normalized > duration:
            raise ValueError(f"{field}={normalized:g} 超出视频时长 {duration:g} 秒")
        supplied[field] = normalized

    effective_start = supplied.get("task_start_time", DEFAULT_TASK_START_TIME)
    task_end = supplied.get("task_end_time")
    if task_end is not None and task_end <= effective_start:
        raise ValueError("task_end_time 必须大于 task_start_time")

    cache_payload = {
        "algorithm_version": KEYFRAME_ALGORITHM_VERSION,
        "task_start_time": effective_start,
        "task_end_time": task_end,
    }
    cache_key = json.dumps(
        cache_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return supplied, cache_key


def _rich_content_timing(
    item: dict,
    duration: float,
    profile: VisualModeProfile,
) -> tuple[dict, str]:
    """校验富内容视频时间窗，并构造专用抽帧配置和缓存键。"""
    supplied: dict[str, float] = {}
    for field in _CONTENT_TIME_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        if normalized < 0:
            raise ValueError(f"{field} 不能小于 0")
        if normalized > duration:
            raise ValueError(f"{field}={normalized:g} 超出视频时长 {duration:g} 秒")
        supplied[field] = normalized

    extraction = profile.extraction
    start = supplied.get("content_start_time", extraction.default_start_time)
    end = supplied.get("content_end_time")
    if end is not None and end <= start:
        raise ValueError("content_end_time 必须大于 content_start_time")

    config = KeyframeConfig(
        task_start_time=start,
        task_end_time=end,
        max_frames=extraction.max_frames,
        sample_fps=extraction.sample_fps,
        scene_threshold=extraction.scene_threshold,
        scene_min_gap_s=extraction.scene_min_gap_s,
        state_layout_threshold=extraction.state_layout_threshold,
        stable_min_duration_s=extraction.stable_min_duration_s,
        max_edge=extraction.max_edge,
        # 问答视频通常不会返回操作助手外壳，禁用操作类的自动结束点推断。
        auto_task_end_confidence_threshold=2.0,
        final_dedup_rms_threshold=0.004,
        final_dedup_changed_fraction_threshold=0.004,
    )
    cache_payload = {
        "algorithm_version": extraction.algorithm_version,
        "config": {
            "content_start_time": start,
            "content_end_time": end,
            "max_frames": extraction.max_frames,
            "sample_fps": extraction.sample_fps,
            "scene_threshold": extraction.scene_threshold,
            "scene_min_gap_s": extraction.scene_min_gap_s,
            "state_layout_threshold": extraction.state_layout_threshold,
            "stable_min_duration_s": extraction.stable_min_duration_s,
            "max_edge": extraction.max_edge,
        },
    }
    cache_key = json.dumps(
        cache_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "config": config,
        "algorithm_version": extraction.algorithm_version,
    }, cache_key


def _prepared_item(item: dict, video_path: Path, frames: list[Path], duration: float) -> dict:
    prepared = dict(item)
    prepared.update({
        "video_path": str(video_path),
        "video_name": video_path.name,
        "media": [str(video_path)],
        "frames": [str(frame) for frame in frames],
        "frame_count": len(frames),
        "duration": round(duration, 2),
    })
    return prepared


def prepare_cached_operation_item(
    item: dict,
    *,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
    probe_fn: Callable = probe_duration,
    extract_fn: Callable = extract_scene_keyframes,
) -> dict:
    """兼容旧准备接口：按视频内容状态复用缓存。"""
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        raise ValueError("缺少 video_path")
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key = _operation_timing(item, duration)
    stat = video_path.stat()
    fingerprint = (
        f"{video_path}:{stat.st_size}:{stat.st_mtime_ns}:"
        f"{cache_key}"
    )
    cache_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
    frame_dir = runs_dir / "videos" / "imported" / f"{cache_id}_frames"
    frames = _extract_frames(
        video_path,
        frame_dir,
        extract_fn=extract_fn,
        cache_key=cache_key,
        extract_kwargs=extract_kwargs,
    )
    if not frames:
        raise ValueError(f"视频抽帧失败：{raw_path}")
    return _prepared_item(item, video_path, frames, duration)


def prepare_session_operation_item(
    item: dict,
    *,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
    probe_fn: Callable = probe_duration,
    extract_fn: Callable = extract_scene_keyframes,
) -> dict:
    """按 Web 历史会话名和题目序号准备关键帧。"""
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        media = item.get("media") or []
        raw_path = str(media[0]).strip() if media else ""
    if not raw_path:
        raise ValueError("缺少 video_path")
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key = _operation_timing(item, duration)

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(str(item.get("id") or f"q{item_index + 1}"), f"q{item_index + 1}")
    safe_session = _safe_name(session_name, "operation_session")
    frame_dir = runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    frames = _extract_frames(
        video_path,
        frame_dir,
        extract_fn=extract_fn,
        cache_key=cache_key,
        extract_kwargs=extract_kwargs,
    )
    if not frames:
        raise ValueError(f"视频抽帧失败：{raw_path}")
    return _prepared_item(item, video_path, frames, duration)


def prepare_session_rich_content_item(
    item: dict,
    *,
    profile: VisualModeProfile,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
    probe_fn: Callable = probe_duration,
    extract_fn: Callable = extract_scene_keyframes,
) -> dict:
    """按 Web 会话准备挂卡 / Superlink 视觉评估关键帧。"""
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        media = item.get("media") or []
        raw_path = str(media[0]).strip() if media else ""
    if not raw_path:
        raise ValueError("缺少 video_path")
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key = _rich_content_timing(item, duration, profile)

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(
        str(item.get("id") or f"q{item_index + 1}"),
        f"q{item_index + 1}",
    )
    safe_session = _safe_name(session_name, "rich_content_session")
    frame_dir = (
        runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    )
    frames = _extract_frames(
        video_path,
        frame_dir,
        extract_fn=extract_fn,
        cache_key=cache_key,
        extract_kwargs=extract_kwargs,
    )
    if not frames:
        raise ValueError(f"视频抽帧失败：{raw_path}")
    return _prepared_item(item, video_path, frames, duration)
