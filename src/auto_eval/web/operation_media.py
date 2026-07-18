"""操作类录屏路径校验与关键帧准备。"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable

from ..media import extract_scene_keyframes, probe_duration
from ..paths import PROJECT_ROOT, RUNS_DIR


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


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


def _cached_frames(frame_dir: Path) -> list[Path]:
    marker = frame_dir / ".complete"
    if not marker.exists():
        return []
    try:
        expected = int(marker.read_text(encoding="utf-8").strip())
        frames = sorted(frame_dir.glob("kf_*.jpg"))
        return frames if expected > 0 and len(frames) == expected else []
    except (OSError, ValueError):
        return []


def _extract_frames(
    video_path: Path,
    frame_dir: Path,
    *,
    extract_fn: Callable = extract_scene_keyframes,
) -> list[Path]:
    frames = _cached_frames(frame_dir)
    if frames:
        return frames
    frame_dir.mkdir(parents=True, exist_ok=True)
    for stale in frame_dir.glob("kf_*.jpg"):
        stale.unlink(missing_ok=True)
    (frame_dir / ".complete").unlink(missing_ok=True)
    frames = list(extract_fn(video_path, frame_dir))
    if frames:
        (frame_dir / ".complete").write_text(str(len(frames)), encoding="utf-8")
    return frames


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
    stat = video_path.stat()
    fingerprint = (
        f"{video_path}:{stat.st_size}:{stat.st_mtime_ns}:"
        "scene-v1-max10-min4-threshold0.10-gap0.8-edge720"
    )
    cache_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
    frame_dir = runs_dir / "videos" / "imported" / f"{cache_id}_frames"
    frames = _extract_frames(video_path, frame_dir, extract_fn=extract_fn)
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

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(str(item.get("id") or f"q{item_index + 1}"), f"q{item_index + 1}")
    safe_session = _safe_name(session_name, "operation_session")
    frame_dir = runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    frames = _extract_frames(video_path, frame_dir, extract_fn=extract_fn)
    if not frames:
        raise ValueError(f"视频抽帧失败：{raw_path}")
    return _prepared_item(item, video_path, frames, duration)
