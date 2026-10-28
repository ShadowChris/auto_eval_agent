"""视频视觉评估的路径校验、缓存与关键帧准备。"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from numbers import Real
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

from ..media import (
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    extract_scene_keyframes,
    probe_duration,
)
from ..config import VisualModeProfile
from ..paths import PROJECT_ROOT, RUNS_DIR


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_TASK_TIME_FIELDS = ("task_start_time", "task_end_time")
_TEMPORARY_VIDEO_PATHS_FIELD = "_temporary_video_paths"
_VIDEO_CONTENT_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", value.strip())
    return safe or fallback


def is_remote_video_url(value: str) -> bool:
    """仅将 http/https URL 视为远程视频，本地路径继续走原有校验。"""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _remote_video_name(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return name or "remote_video.mp4"


def _remote_video_suffix(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return suffix
    media_type = content_type.split(";", 1)[0].strip().lower()
    return _VIDEO_CONTENT_SUFFIXES.get(media_type, ".mp4")


def download_remote_video(
    url: str,
    output_dir: Path,
    *,
    timeout_s: float | None = None,
) -> Path:
    """以流式下载远程视频到运行目录，返回可用于抽帧的本地临时文件。"""
    if not is_remote_video_url(url):
        raise ValueError(f"不支持的视频网络地址：{url}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if timeout_s is None:
        try:
            timeout_s = float(
                os.getenv("AUTO_EVAL_VIDEO_DOWNLOAD_TIMEOUT_S", "120")
            )
        except ValueError:
            timeout_s = 120.0
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        timeout_s = 120.0
    timeout = httpx.Timeout(
        connect=min(30.0, timeout_s),
        read=timeout_s,
        write=timeout_s,
        pool=min(30.0, timeout_s),
    )
    part_path: Path | None = None
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            suffix = _remote_video_suffix(
                str(response.url), response.headers.get("content-type", "")
            )
            stem = _safe_name(Path(_remote_video_name(url)).stem, "video")[:80]
            final_path = output_dir / f"{stem}_{uuid.uuid4().hex}{suffix}"
            part_path = output_dir / f".{final_path.name}.part"
            size = 0
            with part_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    output.write(chunk)
                    size += len(chunk)
            if size <= 0:
                raise ValueError(f"下载的视频为空：{url}")
            part_path.replace(final_path)
            return final_path
    except httpx.HTTPError as exc:
        raise ValueError(f"下载网络视频失败：{url}（{exc}）") from exc
    finally:
        if part_path is not None:
            part_path.unlink(missing_ok=True)


def _remove_temporary_files(paths: list[Path], runs_dir: Path) -> None:
    root = runs_dir.resolve()
    for path in paths:
        try:
            candidate = path.resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def cleanup_item_video_downloads(
    item: dict,
    *,
    runs_dir: Path = RUNS_DIR,
) -> None:
    """删除系统为单题下载的临时视频，不影响用户提供的本地文件。"""
    raw_paths = item.pop(_TEMPORARY_VIDEO_PATHS_FIELD, None) or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    paths = [Path(str(path)) for path in raw_paths if str(path).strip()]
    _remove_temporary_files(paths, runs_dir)


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


def _rich_content_timing(
    item: dict,
    duration: float,
    profile: VisualModeProfile,
    video_path: str = "",
) -> tuple[dict, str]:
    """校验富内容视频时间窗，并构造专用抽帧配置和缓存键。

    缓存键包含视频路径：同一 frame_dir（同 session/index/id）换视频重评时，
    旧 ``.complete`` 标记因 key 不匹配自动失效重抽，避免评到旧帧。
    """
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

    extraction = profile.extraction
    start = supplied.get("task_start_time", extraction.default_start_time)
    end = supplied.get("task_end_time")
    if end is not None and end <= start:
        raise ValueError("task_end_time 必须大于 task_start_time")

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
        # 问答视频通常不会返回操作助手外壳，禁用任务类（录屏）的自动结束点推断。
        auto_task_end_confidence_threshold=2.0,
        final_dedup_rms_threshold=0.004,
        final_dedup_changed_fraction_threshold=0.004,
        # 垂域视觉（问答视频）不需要开头/结尾受保护采样。
        protected_sample_interval=0.0,
    )
    cache_payload = {
        "algorithm_version": extraction.algorithm_version,
        "video": video_path,
        "config": {
            "task_start_time": start,
            "task_end_time": end,
            "max_frames": extraction.max_frames,
            "sample_fps": extraction.sample_fps,
            "scene_threshold": extraction.scene_threshold,
            "scene_min_gap_s": extraction.scene_min_gap_s,
            "state_layout_threshold": extraction.state_layout_threshold,
            "stable_min_duration_s": extraction.stable_min_duration_s,
            "max_edge": extraction.max_edge,
            # 快速变化段（fast-change-span）代表帧保留门槛：参与缓存键，改动时应使旧缓存失效
            "stable_mid_keep_rms": config.stable_mid_keep_rms,
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


def _prepared_item(
    item: dict,
    video_path: Path,
    frames: list[Path],
    duration: float,
    *,
    source_video_path: str = "",
    temporary_video_paths: list[Path] | None = None,
) -> dict:
    public_video_path = source_video_path.strip() or str(video_path)
    prepared = dict(item)
    prepared.update({
        "video_path": public_video_path,
        "video_name": (
            _remote_video_name(public_video_path)
            if is_remote_video_url(public_video_path)
            else video_path.name
        ),
        "media": [public_video_path],
        "frames": [str(frame) for frame in frames],
        "frame_count": len(frames),
        "duration": round(duration, 2),
    })
    if temporary_video_paths:
        prepared[_TEMPORARY_VIDEO_PATHS_FIELD] = [
            str(path) for path in temporary_video_paths
        ]
    return prepared


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
    download_fn: Callable[[str, Path], Path] = download_remote_video,
) -> dict:
    """按 Web 会话准备垂域视觉评测关键帧。"""
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        media = item.get("media") or []
        raw_path = str(media[0]).strip() if media else ""
    if not raw_path:
        raise ValueError("缺少 video_path")

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(
        str(item.get("id") or f"q{item_index + 1}"),
        f"q{item_index + 1}",
    )
    safe_session = _safe_name(session_name, "rich_content_session")
    temporary_video_paths: list[Path] = []
    try:
        if is_remote_video_url(raw_path):
            video_path = download_fn(
                raw_path,
                runs_dir / "downloads" / safe_session,
            ).resolve()
            temporary_video_paths.append(video_path)
        else:
            video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
        duration = float(probe_fn(video_path))
        if duration <= 0:
            raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
        extract_kwargs, cache_key = _rich_content_timing(
            item,
            duration,
            profile,
            video_path=raw_path if is_remote_video_url(raw_path) else str(video_path),
        )
    except Exception:
        _remove_temporary_files(temporary_video_paths, runs_dir)
        raise

    frame_dir = (
        runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    )
    try:
        frames = _extract_frames(
            video_path,
            frame_dir,
            extract_fn=extract_fn,
            cache_key=cache_key,
            extract_kwargs=extract_kwargs,
        )
        if not frames:
            raise ValueError(f"视频抽帧失败：{raw_path}")
    except Exception:
        _remove_temporary_files(temporary_video_paths, runs_dir)
        raise
    return _prepared_item(
        item,
        video_path,
        frames,
        duration,
        source_video_path=raw_path if is_remote_video_url(raw_path) else "",
        temporary_video_paths=temporary_video_paths,
    )


def prepare_session_visual_compare_item(
    item: dict,
    *,
    profile,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
    probe_fn: Callable = probe_duration,
    extract_fn: Callable = extract_scene_keyframes,
    download_fn: Callable[[str, Path], Path] = download_remote_video,
) -> dict:
    """按 Web 会话准备垂域视觉对比评测的双视频关键帧。

    为两个视频分别抽帧，帧路径存入 frames1 / frames2。
    """
    raw_path1 = str(item.get("video1") or "").strip()
    if not raw_path1:
        raise ValueError("缺少 video1")
    raw_path2 = str(item.get("video2") or "").strip()
    if not raw_path2:
        raise ValueError("缺少 video2")

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(
        str(item.get("id") or f"q{item_index + 1}"),
        f"q{item_index + 1}",
    )
    safe_session = _safe_name(session_name, "visual_compare_session")
    temporary_video_paths: list[Path] = []
    try:
        if is_remote_video_url(raw_path1):
            video_path1 = download_fn(
                raw_path1,
                runs_dir / "downloads" / safe_session,
            ).resolve()
            temporary_video_paths.append(video_path1)
        else:
            video_path1 = resolve_operation_video_path(raw_path1, base_dir=base_dir)
        duration1 = float(probe_fn(video_path1))
        if duration1 <= 0:
            raise ValueError(f"无法读取视频1或视频时长为0：{raw_path1}")

        if is_remote_video_url(raw_path2):
            video_path2 = download_fn(
                raw_path2,
                runs_dir / "downloads" / safe_session,
            ).resolve()
            temporary_video_paths.append(video_path2)
        else:
            video_path2 = resolve_operation_video_path(raw_path2, base_dir=base_dir)
        duration2 = float(probe_fn(video_path2))
        if duration2 <= 0:
            raise ValueError(f"无法读取视频2或视频时长为0：{raw_path2}")

        # 使用与 rich_content 相同的抽帧参数；缓存键分别带上各自视频路径，
        # 同目录换视频后各自独立失效重抽。
        extract_kwargs, cache_key1 = _rich_content_timing(
            item,
            duration1,
            profile,
            video_path=raw_path1 if is_remote_video_url(raw_path1) else str(video_path1),
        )
        _, cache_key2 = _rich_content_timing(
            item,
            duration1,
            profile,
            video_path=raw_path2 if is_remote_video_url(raw_path2) else str(video_path2),
        )
    except Exception:
        _remove_temporary_files(temporary_video_paths, runs_dir)
        raise

    base_frame_dir = (
        runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    )

    try:
        frame_dir1 = base_frame_dir / "video1"
        frames1 = _extract_frames(
            video_path1, frame_dir1,
            extract_fn=extract_fn,
            cache_key=cache_key1,
            extract_kwargs=extract_kwargs,
        )
        if not frames1:
            raise ValueError(f"视频1抽帧失败：{raw_path1}")

        frame_dir2 = base_frame_dir / "video2"
        frames2 = _extract_frames(
            video_path2, frame_dir2,
            extract_fn=extract_fn,
            cache_key=cache_key2,
            extract_kwargs=extract_kwargs,
        )
        if not frames2:
            raise ValueError(f"视频2抽帧失败：{raw_path2}")
    except Exception:
        _remove_temporary_files(temporary_video_paths, runs_dir)
        raise

    prepared = dict(item)
    prepared.update({
        "video1": raw_path1 if is_remote_video_url(raw_path1) else str(video_path1),
        "video2": raw_path2 if is_remote_video_url(raw_path2) else str(video_path2),
        "video1_path": str(video_path1),
        "video2_path": str(video_path2),
        "video_name": (
            _remote_video_name(raw_path1)
            if is_remote_video_url(raw_path1)
            else video_path1.name
        ),
        "media": [
            raw_path1 if is_remote_video_url(raw_path1) else str(video_path1),
            raw_path2 if is_remote_video_url(raw_path2) else str(video_path2),
        ],
        "frames1": [str(frame) for frame in frames1],
        "frames2": [str(frame) for frame in frames2],
        "frame_count": len(frames1) + len(frames2),
        "duration": round(duration1, 2),
        "duration1": round(duration1, 2),
        "duration2": round(duration2, 2),
    })
    if temporary_video_paths:
        prepared[_TEMPORARY_VIDEO_PATHS_FIELD] = [
            str(path) for path in temporary_video_paths
        ]
    return prepared
