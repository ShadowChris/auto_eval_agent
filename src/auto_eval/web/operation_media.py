"""视频视觉评估的路径校验、缓存与关键帧准备。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from numbers import Real
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

import httpx

from ..media import (
    DEFAULT_TASK_START_TIME,
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    extract_scene_keyframes,
    probe_duration,
)
from ..config import VisualModeProfile
from ..paths import PROJECT_ROOT, RUNS_DIR
from ..query_images import attachment_image_fields, attachment_image_urls
from .parse_input import operation_query_images
from .remote_video import is_remote_video_url, local_video_input


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
QUERY_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_QUERY_IMAGES = 4
MAX_QUERY_IMAGE_BYTES = 10 * 1024 * 1024
MAX_QUERY_IMAGE_PIXELS = 25_000_000
QUERY_IMAGE_DOWNLOAD_TIMEOUT_S = 30
QUERY_IMAGE_DOWNLOAD_TOTAL_S = 60
_TASK_TIME_FIELDS = ("task_start_time", "task_end_time")
_CONTENT_TIME_FIELDS = ("content_start_time", "content_end_time")


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", value.strip())
    return safe or fallback


def operation_video_roots(base_dir: Path = PROJECT_ROOT) -> list[Path]:
    """返回批量清单允许读取、解析相对路径的视频根目录。"""
    roots = [base_dir.resolve()]
    for raw in os.getenv("OPERATION_VIDEO_ROOTS", "").split(os.pathsep):
        if raw.strip():
            root = Path(raw.strip()).expanduser()
            if not root.is_absolute():
                root = base_dir / root
            roots.append(root.resolve())
    return list(dict.fromkeys(roots))


def resolve_operation_video_path(
    raw_path: str,
    *,
    base_dir: Path = PROJECT_ROOT,
) -> Path:
    """解析本地视频路径，并阻止读取未授权目录。"""
    candidate = Path(raw_path).expanduser()
    roots = operation_video_roots(base_dir)
    if candidate.is_absolute():
        candidate = candidate.resolve()
        if not any(candidate.is_relative_to(root) for root in roots):
            raise ValueError(
                "视频路径不在允许目录中；外部目录需通过 "
                "OPERATION_VIDEO_ROOTS 配置"
            )
    else:
        attempted: list[Path] = []
        matches: list[Path] = []
        for root in roots:
            resolved = (root / candidate).resolve()
            if not resolved.is_relative_to(root):
                continue
            attempted.append(resolved)
            if resolved.is_file():
                matches.append(resolved)
        if not matches:
            locations = "\n".join(f"- {path}" for path in attempted)
            suffix = f"；已按以下根目录查找：\n{locations}" if locations else ""
            raise ValueError(f"视频文件不存在：{raw_path}{suffix}")
        if len(matches) > 1:
            locations = "\n".join(f"- {path}" for path in matches)
            raise ValueError(
                f"视频相对路径存在多个匹配：{raw_path}\n{locations}\n"
                "请提供更完整的相对路径或绝对路径"
            )
        candidate = matches[0]
    if not candidate.is_file():
        raise ValueError(f"视频文件不存在：{raw_path}")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"不支持的视频格式 {candidate.suffix or '(无扩展名)'}；支持：{supported}")
    return candidate


def resolve_operation_query_image_path(
    raw_path: str,
    *,
    base_dir: Path = PROJECT_ROOT,
) -> Path:
    """读取可信用户的图片链接；本地图片仍按原有媒体根目录校验。"""
    raw_path = raw_path.strip()
    if raw_path.lower().startswith(("http://", "https://")):
        return _download_operation_query_image(raw_path, base_dir=base_dir)
    candidate = Path(raw_path).expanduser()
    roots = operation_video_roots(base_dir)
    if candidate.is_absolute():
        candidate = candidate.resolve()
        if not any(candidate.is_relative_to(root) for root in roots):
            raise ValueError(
                "用户输入图片不在允许目录中；外部目录需通过 "
                "OPERATION_VIDEO_ROOTS 配置"
            )
    else:
        matches = [
            resolved
            for root in roots
            if (resolved := (root / candidate).resolve()).is_relative_to(root)
            and resolved.is_file()
        ]
        if not matches:
            raise ValueError(f"用户输入图片不存在：{raw_path}")
        if len(matches) > 1:
            raise ValueError(f"用户输入图片相对路径存在多个匹配：{raw_path}")
        candidate = matches[0]
    if not candidate.is_file():
        raise ValueError(f"用户输入图片不存在：{raw_path}")
    if candidate.suffix.lower() not in QUERY_IMAGE_EXTENSIONS:
        supported = ", ".join(sorted(QUERY_IMAGE_EXTENSIONS))
        raise ValueError(
            f"不支持的用户输入图片格式 {candidate.suffix or '(无扩展名)'}；"
            f"支持：{supported}"
        )
    if candidate.stat().st_size > MAX_QUERY_IMAGE_BYTES:
        raise ValueError("用户输入图片超过 10MB 限制")
    try:
        from PIL import Image

        with Image.open(candidate) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_QUERY_IMAGE_PIXELS:
                raise ValueError("用户输入图片尺寸无效或像素数超过 2500 万")
            image.verify()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"用户输入图片无法解析：{raw_path}") from exc
    return candidate


def _download_operation_query_image(url: str, *, base_dir: Path) -> Path:
    """直接下载 HTTP/HTTPS 图片，不做域名或内网限制；保留可供历史预览的副本。"""
    from PIL import Image, UnidentifiedImageError

    directory = base_dir.resolve() / "runs" / "uploads" / "query_images" / "remote"
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    destination: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="remote_", suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            started = time.monotonic()
            with httpx.stream(
                "GET", url, follow_redirects=True,
                timeout=httpx.Timeout(QUERY_IMAGE_DOWNLOAD_TIMEOUT_S, connect=10),
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > MAX_QUERY_IMAGE_BYTES:
                    raise ValueError("用户输入图片超过 10MB 限制")
                size = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > QUERY_IMAGE_DOWNLOAD_TOTAL_S:
                        raise ValueError("用户输入图片下载超时")
                    size += len(chunk)
                    if size > MAX_QUERY_IMAGE_BYTES:
                        raise ValueError("用户输入图片超过 10MB 限制")
                    handle.write(chunk)
        # 用实际图片格式确定后缀，支持带签名参数、无扩展名及跳转的链接。
        with Image.open(temporary) as image:
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
            if suffix is None:
                raise ValueError("不支持的用户输入图片格式；支持 JPEG、PNG、WEBP")
        destination = temporary.with_suffix(suffix)
        temporary.replace(destination)
        result = resolve_operation_query_image_path(str(destination), base_dir=base_dir)
        destination = None  # 校验通过的完整图片留下供模型和历史预览使用。
        return result
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"用户输入图片下载失败：HTTP {exc.response.status_code} {exc.response.reason_phrase}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ValueError("用户输入图片下载超时") from exc
    except httpx.InvalidURL as exc:
        raise ValueError("用户输入图片链接无效") from exc
    except httpx.RequestError as exc:
        raise ValueError(f"用户输入图片下载失败：{type(exc).__name__}") from exc
    except Image.DecompressionBombError as exc:
        raise ValueError("用户输入图片尺寸无效或像素数超过 2500 万") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("用户输入图片无法解析：链接未返回有效图片或本地副本无法保存") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)


def prepare_operation_query_images(
    item: dict,
    *,
    base_dir: Path = PROJECT_ROOT,
) -> dict:
    """把 query_images 归一为已校验的绝对路径，快照中不保存 base64。"""
    prepared = dict(item)
    raw_images = operation_query_images(item)
    attachment_urls = attachment_image_urls(item)
    if len(raw_images) > MAX_QUERY_IMAGES:
        raise ValueError(f"query_images 最多支持 {MAX_QUERY_IMAGES} 张")
    if raw_images:
        paths: list[str] = []
        downloaded: list[Path] = []
        try:
            for raw in raw_images:
                candidates = attachment_urls if len(raw_images) == 1 and attachment_urls[:1] == [raw] else [raw]
                errors: list[str] = []
                for candidate in candidates:
                    try:
                        if candidate in attachment_urls and not candidate.lower().startswith(("http://", "https://")):
                            raise ValueError("附件地址必须是 HTTP/HTTPS 图片链接")
                        path = resolve_operation_query_image_path(candidate, base_dir=base_dir)
                        break
                    except ValueError as exc:
                        if len(candidates) == 1:
                            raise
                        field = next(key for key, value in attachment_image_fields(item).items() if value == candidate)
                        errors.append(f"{field}：{exc}")
                else:
                    raise ValueError("用户输入图片读取失败：" + "；".join(errors))
                paths.append(str(path))
                if candidate.lower().startswith(("http://", "https://")):
                    downloaded.append(path)
        except Exception:
            for path in downloaded:
                path.unlink(missing_ok=True)
            raise
        prepared["query_images"] = paths
    else:
        prepared.pop("query_images", None)
    return prepared


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
) -> tuple[dict[str, float], str, list[str], set[str]]:
    """校验任务起止时间，并生成抽帧参数、缓存键及回退警告。"""
    supplied: dict[str, float] = {}
    warnings: list[str] = []
    ignored_fields: set[str] = set()
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
            fallback = "默认开始时间" if field == "task_start_time" else "默认结束时间"
            warnings.append(
                f"{field}={normalized:g} 秒超出视频时长 {duration:g} 秒，"
                f"已忽略该值并使用{fallback}"
            )
            ignored_fields.add(field)
            continue
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
    return supplied, cache_key, warnings, ignored_fields


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
        # 问答视频通常不会返回操作助手外壳，禁用任务类（录屏）的自动结束点推断。
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


def _prepared_operation_item(
    item: dict,
    video_path: Path,
    frames: list[Path],
    duration: float,
    supplied_times: dict[str, float],
    warnings: list[str],
    ignored_fields: set[str],
) -> dict:
    normalized_item = dict(item)
    for field in ignored_fields:
        normalized_item.pop(field, None)
    normalized_item.update(supplied_times)
    prepared = _prepared_item(normalized_item, video_path, frames, duration)
    if warnings:
        prepared["video_prepare_warnings"] = warnings
    return prepared


def _prepare_remote_operation_item(
    prepare_fn: Callable, item: dict, raw_url: str, *, runs_dir: Path, **kwargs,
) -> dict:
    """同步调用准备后释放原视频；批跑由外层阶段调度持有到单题结束。"""
    with local_video_input(raw_url, runs_dir) as local_path:
        local_item = {**item, "video_path": str(local_path)}
        # 新下载的内容必须重抽，避免同 URL 对象更新后命中旧帧。
        # 模型重跑已有 frames 时不进入此阶段，仍复用已保存帧。
        stat = local_path.stat()
        fingerprint = f"{raw_url}:{stat.st_size}:{stat.st_mtime_ns}"
        local_item["_remote_video_identity"] = hashlib.sha256(fingerprint.encode()).hexdigest()
        prepared = prepare_fn(local_item, runs_dir=runs_dir, **kwargs)
        if prepared.get("frames"):
            metadata_path = Path(prepared["frames"][0]).parent / "keyframes.json"
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["video"] = raw_url
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    prepared.pop("_remote_video_identity", None)
    prepared.update({
        "video_path": raw_url,
        "media": [raw_url],
        "video_name": Path(unquote(urlsplit(raw_url).path)).name or "remote_video.mp4",
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
    item = prepare_operation_query_images(item, base_dir=base_dir)
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        raise ValueError("缺少 video_path")
    if is_remote_video_url(raw_path):
        return _prepare_remote_operation_item(
            prepare_cached_operation_item, item, raw_path, runs_dir=runs_dir,
            base_dir=base_dir, probe_fn=probe_fn, extract_fn=extract_fn,
        )
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key, warnings, ignored_fields = _operation_timing(item, duration)
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
    return _prepared_operation_item(
        item,
        video_path,
        frames,
        duration,
        extract_kwargs,
        warnings,
        ignored_fields,
    )


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
    item = prepare_operation_query_images(item, base_dir=base_dir)
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        media = item.get("media") or []
        raw_path = str(media[0]).strip() if media else ""
    if not raw_path:
        raise ValueError("缺少 video_path")
    if is_remote_video_url(raw_path):
        return _prepare_remote_operation_item(
            prepare_session_operation_item, item, raw_path, runs_dir=runs_dir,
            session_name=session_name, item_index=item_index, total_items=total_items,
            base_dir=base_dir, probe_fn=probe_fn, extract_fn=extract_fn,
        )
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key, warnings, ignored_fields = _operation_timing(item, duration)

    if item.get("_remote_video_identity"):
        cache_key += ":remote:" + item["_remote_video_identity"]

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
    return _prepared_operation_item(
        item,
        video_path,
        frames,
        duration,
        extract_kwargs,
        warnings,
        ignored_fields,
    )


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
    """按 Web 会话准备垂域视觉评测关键帧。"""
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
