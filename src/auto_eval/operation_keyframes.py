"""任务类（录屏）的关键帧抽取，源自 videosample v4.13（c41967d）。

流程：固定频率与变化取图 → 原有时间/内容筛选 →
局部内容与代表图检查 → 按新增内容自适应输出。

FFmpeg 取图，Pillow/NumPy 比较；OpenCV 仅用于最后选图的局部检查。
"""
from __future__ import annotations

from bisect import bisect_left
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import lru_cache, wraps
from numbers import Integral, Real
from pathlib import Path

import numpy as np

from .media import probe_duration, resolve_ffmpeg_executable


KEYFRAME_ALGORITHM_VERSION = "hybrid-state-v4.13"
DEFAULT_TASK_START_TIME = 7.0
DEFAULT_ANALYSIS_MASK_BOX = (.63, .005, .74, .040)

_FEATURES = ContextVar("keyframe_features", default=None)
_FEATURE_KEYS = ContextVar("keyframe_feature_keys", default=None)
_SMALL_FEATURES = ContextVar("keyframe_small_features", default=None)
_ANALYSIS_MASK = ContextVar("keyframe_analysis_mask", default=DEFAULT_ANALYSIS_MASK_BOX)


def _cached_feature(function):
    @wraps(function)
    def cached(path):
        cache = _FEATURES.get()
        if cache is None:
            return function(path)
        small = function.__name__ in {'_layout_signature', '_assistant_shell_signature'}
        if small and _SMALL_FEATURES.get() is not None:
            cache = _SMALL_FEATURES.get()
        capacity = 4096 if small else 2048 if function.__name__ == '_content_signature' else 512
        identities = _FEATURE_KEYS.get()
        identity = str(path)
        if identities is not None:
            if identity not in identities:
                try:
                    # 候选图在单次抽帧中不再改写；字节完全相同的图可以复用同一份特征。
                    identities[identity] = hashlib.sha256(Path(path).read_bytes()).digest()
                except OSError:
                    identities[identity] = identity
                identity_capacity = 2048 if small or function.__name__ == '_content_signature' else 512
                if len(identities) > identity_capacity:
                    identities.popitem(last=False)
            else:
                identities.move_to_end(identity)
            identity = identities[identity]
        key = (function.__name__, identity)
        if key not in cache:
            cache[key] = function(path)
            if len(cache) > capacity:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        return cache[key]
    return cached


def _with_feature_cache(function):
    @wraps(function)
    def cached(*args, **kwargs):
        token = _FEATURES.set(OrderedDict())
        identities_token = _FEATURE_KEYS.set(OrderedDict())
        small_token = _SMALL_FEATURES.set(OrderedDict())
        config = kwargs.get('config') or (args[2] if len(args) > 2 else None)
        mask_token = _ANALYSIS_MASK.set(getattr(config, 'analysis_mask_box', _ANALYSIS_MASK.get()))
        try:
            return function(*args, **kwargs)
        finally:
            _FEATURES.reset(token)
            _FEATURE_KEYS.reset(identities_token)
            _SMALL_FEATURES.reset(small_token)
            _ANALYSIS_MASK.reset(mask_token)
    return cached


def _prewarm_features(candidates, function):
    """并行读取/计算独立特征，结果按原输入顺序写入单次抽帧缓存。"""
    features, identities = _FEATURES.get(), _FEATURE_KEYS.get()
    if features is None or identities is None or len(candidates) < 8:
        return
    small = function.__name__ in {'_layout_signature', '_assistant_shell_signature'}
    cache = _SMALL_FEATURES.get() if small else features
    capacity = 4096 if small else 2048
    paths, seen = [], set()
    for candidate in candidates:
        path = candidate.path
        digest = identities.get(str(path))
        key = digest if isinstance(digest, bytes) else str(path)
        if key in seen or (function.__name__, digest) in cache:
            continue
        seen.add(key)
        paths.append(path)
    if not paths:
        return
    mask_box = _ANALYSIS_MASK.get()
    def load(path):
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).digest()
        mask_token = _ANALYSIS_MASK.set(mask_box)
        try:
            value = function.__wrapped__(io.BytesIO(payload))
        finally:
            _ANALYSIS_MASK.reset(mask_token)
        return path, digest, value
    worker_count = min(8, max(1, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix='frame-feature') as workers:
        for path, digest, value in workers.map(load, paths):
            identities[str(path)] = digest
            identities.move_to_end(str(path))
            if len(identities) > 2048:
                identities.popitem(last=False)
            key = (function.__name__, digest)
            if key not in cache:
                cache[key] = value
                if len(cache) > capacity:
                    cache.popitem(last=False)


@dataclass(frozen=True)
class KeyframeConfig:
    """操作录屏关键帧算法配置。"""

    task_start_time: float = DEFAULT_TASK_START_TIME
    task_end_time: float | None = None
    max_frames: int = 15
    sample_fps: float = 3.0
    scene_threshold: float = 0.02
    scene_min_gap_s: float = 0.15
    timestamp_merge_gap_s: float = 0.25
    state_layout_threshold: float = 0.05
    stable_min_duration_s: float = 1.5
    transient_max_duration_s: float = 4.0
    transient_return_threshold: float = 0.065
    transient_min_changed_fraction: float = 0.02
    transient_max_changed_fraction: float = 0.55
    auto_task_end_min_stable_duration_s: float = 1.5
    auto_task_end_min_final_stable_duration_s: float = 1.5
    auto_task_end_max_return_shell_distance: float = 0.02
    auto_task_end_min_task_shell_distance: float = 0.02
    auto_task_end_confidence_threshold: float = 0.60
    final_dedup_rms_threshold: float = 0.008
    final_dedup_changed_fraction_threshold: float = 0.01
    max_edge: int = 720
    content_rms_threshold: float = 0.025
    content_changed_fraction: float = 0.06
    content_tile_threshold: float = 0.06
    global_dedup_rms_threshold: float = 0.004
    global_dedup_changed_fraction: float = 0.003
    global_dedup_tile_threshold: float = 0.01
    global_dedup_patch_threshold: float = 0.02
    local_analysis_max_edge: int = 320
    local_template_threshold: float = 0.90
    local_content_bin_height: int = 24
    local_fast_alignment: bool = True
    local_search_reuse: bool = True
    local_match_workers: int = 2
    local_bounded_search: bool = True
    local_reference_prefilter: bool = True
    selection_scoring: str = field(default='new_content_ratio', init=False)
    analysis_mask_box: tuple | None = DEFAULT_ANALYSIS_MASK_BOX
    scroll_dedup_enabled: bool = True
    scroll_analysis_max_edge: int = 800
    scroll_min_width: int = 400
    scroll_min_height: int = 800
    scroll_max_related: int = 3
    scroll_checks_per_candidate: int = 3
    scroll_agent_indicator_box: tuple | None = (.32, .955, .70, .995)
    # 受保护采样：在开头和结尾两个时间窗口内，每 interval 秒额外标记一帧，
    # 仅当画面高度一致时才去重（用于捕获弹窗/横幅/开场状态等开头结尾的小变化）。
    protected_sample_interval: float = 2.0  # 0 = 禁用；>0 则每 N 秒一帧
    protected_begin_window: float = 20.0    # 从 task_start_time 起的前 N 秒
    protected_end_window: float = 5.0       # 从 algorithm_end 止的后 N 秒
    protected_sample_dedup_rms: float = 0.002    # 受保护帧的去重 RMS 阈值
    protected_sample_dedup_cf: float = 0.002      # 受保护帧的去重变化比例阈值

    def __post_init__(self) -> None:
        """规范化并校验任务时间参数。"""
        for name, value in (
            ("task_start_time", self.task_start_time),
            ("task_end_time", self.task_end_time),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(f"{name} 必须是有限数字")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError(f"{name} 必须是有限数字")
            if normalized < 0:
                raise ValueError(f"{name} 不能小于 0")
            object.__setattr__(self, name, normalized)
        if (
            self.task_end_time is not None
            and self.task_end_time <= self.task_start_time
        ):
            raise ValueError("task_end_time 必须大于 task_start_time")
        if isinstance(self.max_frames, bool) or not isinstance(self.max_frames, Integral) or self.max_frames < 2:
            raise ValueError("实验版 max_frames 必须为至少 2 的整数")
        if self.local_analysis_max_edge < 64 or self.local_content_bin_height < 4:
            raise ValueError("局部分析尺寸或内容分段过小")
        if not 0 < self.local_template_threshold <= 1:
            raise ValueError("局部匹配阈值必须在 0 到 1 之间")
        for name in ('local_fast_alignment', 'local_search_reuse', 'local_bounded_search', 'local_reference_prefilter'):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须为布尔值")
        if (isinstance(self.local_match_workers, bool) or not isinstance(self.local_match_workers, Integral)
                or not 1 <= self.local_match_workers <= 4):
            raise ValueError("local_match_workers 必须为 1 到 4 的整数")
        if (isinstance(self.sample_fps, bool) or not isinstance(self.sample_fps, Real)
                or not math.isfinite(self.sample_fps) or self.sample_fps <= 0):
            raise ValueError("sample_fps 必须为大于 0 的有限数字，可以是小数")
        if self.analysis_mask_box is not None:
            box = self.analysis_mask_box
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                    or not all(isinstance(v, Real) and math.isfinite(v) for v in box)
                    or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1):
                raise ValueError("分析遮挡区域必须为 0 到 1 的左、上、右、下坐标，或 None")
            object.__setattr__(self, 'analysis_mask_box', tuple(box))
        if not isinstance(self.scroll_dedup_enabled, bool):
            raise ValueError("scroll_dedup_enabled 必须为布尔值")
        for name, value, lower, upper in (
            ('scroll_analysis_max_edge', self.scroll_analysis_max_edge, 64, 2048),
            ('scroll_min_width', self.scroll_min_width, 1, 8192),
            ('scroll_min_height', self.scroll_min_height, 1, 8192),
            ('scroll_max_related', self.scroll_max_related, 1, 15),
            ('scroll_checks_per_candidate', self.scroll_checks_per_candidate, 1, 15),
        ):
            if isinstance(value, bool) or not isinstance(value, Integral) or not lower <= value <= upper:
                raise ValueError(f"{name} 必须为 {lower} 到 {upper} 的整数")
        if self.scroll_agent_indicator_box is not None:
            box = self.scroll_agent_indicator_box
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                    or not all(isinstance(v, Real) and math.isfinite(v) for v in box)
                    or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1):
                raise ValueError("滑动检查遮挡区域必须为归一化坐标或 None")
            object.__setattr__(self, 'scroll_agent_indicator_box', tuple(box))
        if self.max_edge <= 0:
            raise ValueError("max_edge 必须大于 0")
        if (
            self.protected_sample_interval < 0
            or (self.protected_sample_interval > 0 and self.protected_sample_interval < 0.5)
        ):
            raise ValueError("protected_sample_interval 必须为 0 或 >= 0.5")
        if self.protected_begin_window < 0:
            raise ValueError("protected_begin_window 不能小于 0")
        if self.protected_end_window < 0:
            raise ValueError("protected_end_window 不能小于 0")


@dataclass
class _Candidate:
    time: float
    path: Path
    source: str
    novelty: float = 0.0
    keep_reason: str = ""
    support_s: float = 0.0


def scene_change_times(
    video: Path | str,
    threshold: float = 0.02,
    *,
    end_time: float | None = None,
) -> list[float]:
    """返回 FFmpeg scene 检测到的变化时间点；失败返回空列表。"""
    command = [resolve_ffmpeg_executable(), "-hide_banner", "-i", str(video)]
    if end_time is not None:
        command.extend(["-t", f"{end_time:.3f}"])
    box = _ANALYSIS_MASK.get()
    mask_filter = (f'drawbox=x=iw*{box[0]}:y=ih*{box[1]}:'
                   f'w=iw*{box[2] - box[0]}:h=ih*{box[3] - box[1]}:color=black:t=fill,') if box else ''
    command.extend(
        [
            "-an",
            "-vf",
            f"{mask_filter}select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ]
    )
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return []
    return [
        float(value)
        for value in re.findall(r"pts_time:([0-9.]+)", proc.stderr or "")
    ]


def _dedup(times: list[float], min_gap_s: float) -> list[float]:
    """相邻时间点小于间隔时取先者。"""
    out: list[float] = []
    for timestamp in sorted(times):
        if not out or timestamp - out[-1] >= min_gap_s:
            out.append(timestamp)
    return out


def _extract_at(
    video: Path,
    timestamp: float,
    output: Path,
    *,
    max_edge: int,
) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                resolve_ffmpeg_executable(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({max_edge},iw)':-2",
                "-q:v",
                "3",
                str(output),
            ],
            check=False,
            capture_output=True,
        )
    except Exception:
        return False
    return output.is_file()


def _extract_many_at(
    video: Path, timestamps: list[float], directory: Path, *, max_edge: int,
) -> dict[float, Path]:
    """一次解码取多个时刻；仍取三位小数时间点处或其后的第一帧。"""
    if not timestamps:
        return {}
    directory.mkdir(parents=True, exist_ok=True)
    targets = sorted({float(f"{timestamp:.3f}") for timestamp in timestamps})
    # prev_pts 是前一输入帧，而非前一选中帧。每个目标只命中跨过它的那一帧。
    crossings = "+".join(f"gte(t,{target:.3f})*lt(prev_pts*TB,{target:.3f})"
                         for target in targets)
    expression = f"if(isnan(prev_pts),gte(t,{targets[0]:.3f}),{crossings})"
    script = directory / "select.ffscript"
    script.write_text(f"select='{expression}',showinfo,scale='min({max_edge},iw)':-2", encoding="utf-8")
    try:
        process = subprocess.run(
            [resolve_ffmpeg_executable(), "-y", "-hide_banner", "-i", str(video),
             "-t", f"{targets[-1] + 1:.3f}", "-an", "-filter_script:v", str(script),
             "-vsync", "0", "-q:v", "3", str(directory / "batch_%05d.jpg")],
            check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        times = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", process.stderr or "")]
        paths = sorted(directory.glob("batch_*.jpg"))
        if process.returncode or len(times) != len(paths):
            return {}
        result = {}
        for timestamp in timestamps:
            target = float(f"{timestamp:.3f}")
            index = bisect_left(times, target - 1e-6)
            if index < len(paths):
                result[timestamp] = paths[index]
        return result
    except (OSError, subprocess.SubprocessError):
        return {}


def _masked_analysis_image(image, box):
    """只修改 convert 生成的内存副本，不改变候选和最终输出图片。"""
    if box is not None:
        width, height = image.size
        x0, y0, x1, y1 = box
        image.paste(0, (round(x0 * width), round(y0 * height), round(x1 * width), round(y1 * height)))
    return image


@_cached_feature
def _signature(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as original:
        image = _masked_analysis_image(original.convert('L'), _ANALYSIS_MASK.get()).resize((96, 160))
    array = np.asarray(image, dtype=np.float32)
    return array[12:154, :]


@_cached_feature
def _layout_signature(path: Path) -> np.ndarray:
    from PIL import Image, ImageFilter

    with Image.open(path) as original:
        image = _masked_analysis_image(original.convert('L'), _ANALYSIS_MASK.get())
    image = image.filter(ImageFilter.GaussianBlur(radius=12)).resize((16, 27))
    array = np.asarray(image, dtype=np.float32)
    return array[2:-1, :]


@_cached_feature
def _assistant_shell_signature(path: Path) -> np.ndarray:
    from PIL import Image, ImageFilter

    with Image.open(path) as original:
        image = _masked_analysis_image(original.convert('L'), _ANALYSIS_MASK.get())
    image = image.filter(ImageFilter.GaussianBlur(radius=8)).resize((32, 54))
    array = np.asarray(image, dtype=np.float32)
    return array[-8:-2, :]


def _visual_difference(
    left: np.ndarray,
    right: np.ndarray,
) -> tuple[float, float]:
    delta = np.abs(left - right)
    rms = float(np.sqrt(np.mean(delta * delta)) / 255.0)
    changed_fraction = float(np.mean(delta >= 12.0))
    return rms, changed_fraction


@_cached_feature
def _content_signature(path: Path) -> np.ndarray:
    """相近时刻合并时看完整彩色画面，避免漏掉顶部提示或颜色变化。"""
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(_masked_analysis_image(image.convert('RGB'), _ANALYSIS_MASK.get()).resize((96, 160)), dtype=np.uint8)


def _content_difference(left: np.ndarray, right: np.ndarray) -> tuple[float, float]:
    delta = np.abs(left.astype(np.float32) - right.astype(np.float32))
    return (float(np.sqrt(np.mean(delta * delta)) / 255.0),
            float(np.mean(np.any(delta >= 12.0, axis=-1))))


def _layout_distance(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.sqrt(np.mean((left - right) ** 2)) / 255.0)


def _extract_candidates(
    video: Path,
    output_dir: Path,
    config: KeyframeConfig,
    duration: float,
) -> tuple[float | None, list[_Candidate]]:
    effective_start = min(
        max(0.0, config.task_start_time),
        max(0.0, duration - 0.5),
    )
    effective_task_end: float | None = None
    if config.task_end_time is not None:
        effective_task_end = min(
            max(effective_start, config.task_end_time),
            max(effective_start, duration - 0.3),
        )
    algorithm_end = effective_task_end or max(effective_start, duration - 0.3)
    sample_start = min(duration, effective_start + 1.0 / config.sample_fps)

    every_second_dir = output_dir / "sampled"
    every_second_dir.mkdir(parents=True, exist_ok=True)
    if algorithm_end > sample_start:
        try:
            subprocess.run(
                [
                    resolve_ffmpeg_executable(),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{sample_start:.3f}",
                    "-i",
                    str(video),
                    "-t",
                    f"{algorithm_end - sample_start:.3f}",
                    "-vf",
                    (
                        f"fps={config.sample_fps:.17g},"
                        f"scale='min({config.max_edge},iw)':-2"
                    ),
                    "-q:v",
                    "3",
                    str(every_second_dir / "frame_%05d.jpg"),
                ],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass

    interval = 1.0 / config.sample_fps
    candidates = [
        _Candidate(
            sample_start + (index - 1) * interval,
            path,
            f"{config.sample_fps:g}fps",
        )
        for index, path in enumerate(
            sorted(every_second_dir.glob("frame_*.jpg")),
            start=1,
        )
    ]

    # 受保护采样：在开头 [start, start+begin_window] 和结尾 [end-end_window, end]
    # 两个窗口内，每隔 protected_sample_interval 秒将对应帧标记为 protected，
    # 使其在后续各阶段优先保留，仅当画面高度一致时才去重
    # （用于捕获弹窗/横幅/开场状态等开头结尾的小变化）。
    fps_source = f"{config.sample_fps:g}fps"
    ps_interval = config.protected_sample_interval
    if ps_interval > 0 and (config.protected_begin_window > 0 or config.protected_end_window > 0):
        protected_begin_end = effective_start + config.protected_begin_window
        protected_end_start = algorithm_end - config.protected_end_window
        for c in candidates:
            if c.source != fps_source:
                continue
            protected = False
            # 开头窗口：[effective_start, effective_start + protected_begin_window]
            # 帧时间相对起点的偏移能被 interval 整除则保留
            if config.protected_begin_window > 0 and effective_start <= c.time <= protected_begin_end:
                offset = (c.time - effective_start) % ps_interval
                if min(offset, ps_interval - offset) <= interval / 2 + 1e-6:
                    protected = True
            # 结尾窗口：[algorithm_end - protected_end_window, algorithm_end]
            # 帧时间相对终点的偏移能被 interval 整除则保留
            if not protected and config.protected_end_window > 0 and protected_end_start <= c.time <= algorithm_end:
                offset = (algorithm_end - c.time) % ps_interval
                if min(offset, ps_interval - offset) <= interval / 2 + 1e-6:
                    protected = True
            if protected:
                c.source = f"protected-{config.protected_sample_interval:g}s"

    start_path = output_dir / "start" / f"start_{effective_start:.3f}.jpg"
    if _extract_at(video, effective_start, start_path, max_edge=config.max_edge):
        candidates.append(
            _Candidate(
                effective_start,
                start_path,
                f"start-{effective_start:.1f}s",
            )
        )

    scene_times = _dedup(
        scene_change_times(
            video,
            config.scene_threshold,
            end_time=algorithm_end,
        ),
        config.scene_min_gap_s,
    )
    scene_images = _extract_many_at(
        video, [t for t in scene_times if effective_start <= t <= algorithm_end],
        output_dir / "scene_batch", max_edge=config.max_edge,
    )
    for index, timestamp in enumerate(scene_times, start=1):
        if not effective_start <= timestamp <= algorithm_end:
            continue
        path = output_dir / "scenes" / f"scene_{index:04d}_{timestamp:.3f}.jpg"
        if timestamp in scene_images:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scene_images[timestamp], path)
        elif not _extract_at(video, timestamp, path, max_edge=config.max_edge):
            continue
        if path.is_file():
            candidates.append(
                _Candidate(
                    timestamp,
                    path,
                    f"scene-{config.scene_threshold:g}",
                )
            )

    if effective_task_end is not None:
        task_end_path = (
            output_dir / "task_end" / f"task_end_{effective_task_end:.3f}.jpg"
        )
        if _extract_at(
            video,
            effective_task_end,
            task_end_path,
            max_edge=config.max_edge,
        ):
            candidates.append(
                _Candidate(effective_task_end, task_end_path, "task-end")
            )

    # 显式时间窗可能只是多轮录屏中的一轮，不能混入整段视频的末帧。
    # 未提供结束点时，末帧仍用于自动结束点推断和最终状态兜底。
    if config.task_end_time is None:
        for backoff in (0.3, 0.6, 1.0, 1.5, 2.0):
            timestamp = max(0.0, duration - backoff)
            terminal_path = (
                output_dir / "terminal" / f"terminal_{timestamp:.3f}.jpg"
            )
            if _extract_at(
                video,
                timestamp,
                terminal_path,
                max_edge=config.max_edge,
            ):
                candidates.append(
                    _Candidate(
                        timestamp,
                        terminal_path,
                        f"terminal-{backoff:.1f}s",
                    )
                )
                break

    candidates.sort(
        key=lambda item: (
            item.time,
            0 if item.source.startswith("scene") else 1,
        )
    )
    return effective_task_end, candidates


def _canonicalize_timestamps(
    candidates: list[_Candidate],
    gap: float,
    *,
    config: KeyframeConfig | None = None,
) -> list[_Candidate]:
    """只合并时间接近且画面相似的候选；每组时间范围有界。"""
    if not candidates:
        return []
    config = config or KeyframeConfig()
    signatures = {}

    def signature(candidate):
        if id(candidate) not in signatures:
            signatures[id(candidate)] = (candidate.time, _content_signature(candidate.path))
        return signatures[id(candidate)][1]

    def priority(candidate):
        if candidate.source.startswith("start"):
            return (0, candidate.time)
        if candidate.source == "task-end":
            return (1, candidate.time)
        if candidate.source.startswith("protected"):
            return (2, candidate.time)
        if candidate.source.startswith("scene"):
            return (3, candidate.time)
        return (4, candidate.time)

    def endpoint(candidate):
        return candidate.source.startswith("start") or candidate.source == "task-end"

    groups = []
    for candidate in sorted(candidates, key=lambda c: c.time):
        for identity, (timestamp, _) in list(signatures.items()):
            if candidate.time - timestamp > gap + 1e-9:
                del signatures[identity]
        merged = False
        for group in reversed(groups):
            if candidate.time - group[0].time > gap + 1e-9:
                break
            winner = min(group, key=priority)
            if endpoint(candidate) and any(endpoint(c) for c in group):
                continue
            rms, fraction = _content_difference(signature(candidate), signature(winner))
            if (rms < config.final_dedup_rms_threshold
                    and fraction < config.final_dedup_changed_fraction_threshold):
                group.append(candidate)
                merged = True
                break
        if not merged:
            groups.append([candidate])

    result = []
    for group in groups:
        winner = min(group, key=priority)
        result.append(winner)
    return sorted(result, key=lambda c: c.time)


def _build_layout_runs(
    candidates: list[_Candidate],
    threshold: float,
) -> tuple[list[np.ndarray], list[list[int]]]:
    if not candidates:
        return [], []
    _prewarm_features(candidates, _layout_signature)
    layouts = [_layout_signature(candidate.path) for candidate in candidates]
    runs: list[list[int]] = [[0]]
    for index in range(1, len(candidates)):
        if _layout_distance(layouts[index - 1], layouts[index]) >= threshold:
            runs.append([index])
        else:
            runs[-1].append(index)
    return layouts, runs


def _infer_task_end_candidate(
    candidates: list[_Candidate],
    terminal: _Candidate | None,
    config: KeyframeConfig,
) -> tuple[_Candidate | None, float | None]:
    if len(candidates) < 2 or terminal is None:
        return None, None
    layouts, runs = _build_layout_runs(
        candidates,
        config.state_layout_threshold,
    )
    if len(runs) < 2:
        return None, None

    final_shell = _assistant_shell_signature(terminal.path)
    start_shell = _assistant_shell_signature(candidates[0].path)
    if (
        _layout_distance(start_shell, final_shell)
        > config.auto_task_end_max_return_shell_distance
    ):
        return None, None

    final_run = runs[-1]
    final_run_duration = (
        candidates[final_run[-1]].time - candidates[final_run[0]].time
    )
    final_layout = _layout_signature(terminal.path)
    if (
        final_run_duration
        < config.auto_task_end_min_final_stable_duration_s
        or _layout_distance(layouts[final_run[-1]], final_layout)
        >= config.state_layout_threshold
    ):
        return None, None

    eligible: list[tuple[float, float, list[int]]] = []
    for run in runs[:-1]:
        run_duration = candidates[run[-1]].time - candidates[run[0]].time
        if run_duration < config.auto_task_end_min_stable_duration_s:
            continue
        task_shell = _assistant_shell_signature(candidates[run[-1]].path)
        shell_distance = _layout_distance(task_shell, final_shell)
        if shell_distance <= config.auto_task_end_min_task_shell_distance:
            continue
        layout_distance = _layout_distance(layouts[run[-1]], final_layout)
        stable_score = min(1.0, run_duration / 8.0)
        layout_score = min(
            1.0,
            max(0.0, (layout_distance - 0.04) / 0.08),
        )
        shell_score = min(
            1.0,
            max(0.0, (shell_distance - 0.02) / 0.08),
        )
        confidence = (
            0.40 * stable_score
            + 0.20 * layout_score
            + 0.15 * shell_score
            + 0.15 * min(1.0, final_run_duration / 3.0)
            + 0.10
        )
        if confidence >= config.auto_task_end_confidence_threshold:
            eligible.append((run_duration, confidence, run))

    if not eligible:
        return None, None
    _, confidence, best_run = max(
        eligible,
        key=lambda item: (item[0], item[1], candidates[item[2][-1]].time),
    )
    return candidates[best_run[-1]], confidence


def _tile_difference(left, right):
    delta = np.max(np.abs(left.astype(np.float32) - right.astype(np.float32)), axis=-1) / 255.0
    # 8×6 个局部区域；同页小卡片变化不会被整张页面的平均值冲淡。
    return float(delta.reshape(8, 20, 6, 16).mean(axis=(1, 3)).max())


def _descriptor(pixels):
    return pixels.reshape(20, 8, 12, 8, 3).mean(axis=(1, 3)).astype(np.float32) / 255.0


def _content_groups(candidates, config):
    """按实际内容归组，允许少量像素差异，但检查局部变化。"""
    @lru_cache(maxsize=64)
    def pixels(path):
        return _content_signature(Path(path))

    groups = []
    descriptors = np.empty((len(candidates), 20, 12, 3), dtype=np.float32)
    coarse_descriptors = np.empty((len(candidates), 48), dtype=np.float32)
    for candidate in candidates:
        image = pixels(str(candidate.path))
        descriptor = _descriptor(image)
        found = None
        # 批量做廉价预筛；临界值仍按原标量公式复核，保持组顺序与决定不变。
        coarse = descriptor.reshape(4, 5, 4, 3, 3).mean(axis=(1, 3)).ravel()
        coarse_squared = np.mean((coarse_descriptors[:len(groups)] - coarse) ** 2, axis=1)
        possible = np.flatnonzero(coarse_squared <= (config.content_rms_threshold + 5e-7) ** 2)
        squared = np.mean((descriptors[possible] - descriptor) ** 2, axis=(1, 2, 3))
        possible = possible[squared <= (config.content_rms_threshold + 1e-7) ** 2]
        for index in reversed(possible):
            group = groups[int(index)]
            if float(np.sqrt(np.mean((descriptor - group["descriptor"]) ** 2))) > config.content_rms_threshold:
                continue
            other = pixels(str(group["reference"].path))
            rms, fraction = _content_difference(image, other)
            if (rms < config.content_rms_threshold and fraction < config.content_changed_fraction
                    and _tile_difference(image, other) < config.content_tile_threshold):
                found = group
                break
        if found is None:
            descriptors[len(groups)] = descriptor
            coarse_descriptors[len(groups)] = coarse
            groups.append({"members": [candidate], "representative": candidate, "reference": candidate,
                           "descriptor": descriptor, "support": candidate.support_s})
        else:
            found["members"].append(candidate)
            found["support"] += candidate.support_s
            current = found["representative"]
            if (candidate.support_s, candidate.time) > (current.support_s, current.time):
                found["representative"] = candidate
    return groups


def _deduplicate_states(candidates, config):
    if not candidates:
        return []
    _, runs = _build_layout_runs(candidates, config.state_layout_threshold)
    selected = []
    interval = 1.0 / config.sample_fps
    for index, candidate in enumerate(candidates):
        candidate.support_s = min(interval, max(0.0, candidates[index + 1].time - candidate.time)) if index + 1 < len(candidates) else interval
    for run in runs:
        groups = _content_groups([candidates[index] for index in run], config)
        for group in groups:
            representative = group["representative"]
            representative.support_s = group["support"]
            # 首帧不会因选择更晚的重复图而消失；任务结束帧后续仍独立补回。
            anchors = [c for c in group["members"] if c is candidates[0]]
            kept = anchors or [representative]
            for candidate in group["members"]:
                if candidate in kept:
                    candidate.keep_reason = "content-state"
            selected.extend(kept)
    return sorted(selected, key=lambda c: c.time)


def _merge_repeated_content(candidates, config):
    """分配数量前先合并重复内容；首尾位置仍保留。"""
    if not candidates:
        return []
    selected = []
    for group in _content_groups(candidates, config):
        anchors = [c for c in group["members"] if c is candidates[0] or c.keep_reason in {
            "task-end-explicit", "task-end-auto", "final-frame"}]
        kept = anchors or [group["representative"]]
        for candidate in group["members"]:
            if candidate in kept:
                candidate.support_s = group["support"]
        selected.extend(kept)
    return sorted(selected, key=lambda c: c.time)


@lru_cache(maxsize=1)
def _local_cv():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("任务类抽帧需要 OpenCV，请安装 auto-eval-agent 的 video 依赖") from exc
    # 平台本身并行两题；避免每次小图比较再启动一组原生工作线程。
    cv2.setNumThreads(1)
    return cv2


def _strict_region_equal(left, right, config):
    """同样的四项严格条件，应用于对齐后的局部内容，而非整页平均。"""
    cv = _local_cv()
    coarse_left = cv.resize(left, (8, 8), interpolation=cv.INTER_AREA).astype(np.float32)
    coarse_right = cv.resize(right, (8, 8), interpolation=cv.INTER_AREA).astype(np.float32)
    # 粗平均差异是后续 RMS 的下界；额外保留两级像素量化余量，
    # 明显不同的重叠区不用反复生成较大的比较数组。
    coarse_rms = float(np.sqrt(np.mean((coarse_left - coarse_right) ** 2)) / 255)
    if coarse_rms > config.global_dedup_rms_threshold + 2 / 255:
        return False
    left = cv.resize(left, (96, 160), interpolation=cv.INTER_AREA).astype(np.float32)
    right = cv.resize(right, (96, 160), interpolation=cv.INTER_AREA).astype(np.float32)
    delta = np.abs(left - right)
    if float(np.sqrt(np.mean(delta * delta)) / 255) >= config.global_dedup_rms_threshold:
        return False
    if float(np.mean(np.any(delta >= 12, axis=-1))) >= config.global_dedup_changed_fraction:
        return False
    maximum = delta.max(axis=-1) / 255
    return (float(maximum.reshape(8, 20, 6, 16).mean(axis=(1, 3)).max()) < config.global_dedup_tile_threshold
            and float(maximum.reshape(40, 4, 24, 4).mean(axis=(1, 3)).max()) < config.global_dedup_patch_threshold)


def _local_boxes(rgb):
    """颜色块和矩形轮廓只是关注区域线索，不是业务卡片识别。"""
    cv = _local_cv()
    height, width = rgb.shape[:2]
    corners = np.concatenate([rgb[:4, :4].reshape(-1, 3), rgb[:4, -4:].reshape(-1, 3),
                              rgb[-4:, :4].reshape(-1, 3), rgb[-4:, -4:].reshape(-1, 3)])
    background = np.median(corners, axis=0).astype(np.uint8)
    foreground = (np.max(np.abs(rgb.astype(np.int16) - background), axis=2) >= 18).astype(np.uint8)
    foreground = cv.morphologyEx(foreground, cv.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv.connectedComponentsWithStats(foreground, connectivity=8)
    boxes = []
    for x, y, w, h, area in stats[1:count]:
        if area / max(1, w * h) >= .55:
            boxes.append((int(x), int(y), int(w), int(h)))
    gray = cv.cvtColor(rgb, cv.COLOR_RGB2GRAY)
    contours, _ = cv.findContours(cv.Canny(gray, 60, 160), cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv.boundingRect(contour)
        if cv.contourArea(contour) >= .80 * w * h:
            boxes.append((x, y, w, h))
    accepted = []
    for box in sorted(set(boxes), key=lambda b: (-b[2] * b[3], b)):
        x, y, w, h = box
        area = w * h / (width * height)
        if w < .30 * width or h < .08 * height or not .035 <= area <= .93:
            continue
        if any(max(0, min(x + w, a + c) - max(x, a)) * max(0, min(y + h, b + d) - max(y, b))
               >= .8 * min(w * h, c * d) for a, b, c, d in accepted):
            continue
        accepted.append(box)
        if len(accepted) == 4:
            break
    return accepted, background


@dataclass
class _LocalTrack:
    index: int
    pixels: np.ndarray
    origin: int
    width_ratio: float
    kind: str
    last_index: int
    observations: list = field(default_factory=list)
    bins: list | None = None
    color: np.ndarray | None = None
    gray: np.ndarray | None = None
    reliable: bool = True
    search_data: object = None
    row_summary: object = None


@dataclass
class _LocalAnalysis:
    candidates: list
    representatives: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    background_ids: dict = field(default_factory=dict)
    descriptors: dict = field(default_factory=dict)


class _LocalRows:
    """原 RGB 区域的八列面积均值与纵向积分，不保留原图。"""
    def __init__(self, pixels):
        cv = _local_cv()
        means = cv.resize(pixels[:, 2:-2].astype(np.float32), (8, len(pixels)),
                          interpolation=cv.INTER_AREA).astype(np.float64)
        self.rows = np.concatenate([means, np.zeros((1, 8, 3))])
        self.prefix = np.concatenate([np.zeros((1, 8, 3)), np.cumsum(means, axis=0)])
        self.height = len(pixels)

    def integral(self, positions):
        whole = np.floor(positions).astype(np.intp)
        return self.prefix[whole] + (positions - whole)[..., None, None] * self.rows[whole]

    def possibly_matches(self, other, config):
        minimum = max(8, math.ceil(min(self.height, other.height) * .40))
        if minimum > min(self.height, other.height):
            return True
        positions = np.arange(-other.height + minimum, self.height - minimum + 1)
        top = np.maximum(positions, 0)
        length = np.minimum(self.height, positions + other.height) - top
        other_top = top - positions
        # 原核对先比较 8×8 的 RGB RMS，门槛为 global + 2/255。
        # 这里合并部分行只会降低 RMS；再给两级像素余量覆盖面积均值与量化误差。
        cutoff = (config.global_dedup_rms_threshold + 4 / 255 + 1e-9) * 255
        for bins in (1, 2, 4, 8):
            boundaries = np.linspace(0, 1, bins + 1)
            left = self.integral(top[:, None] + length[:, None] * boundaries)
            right = other.integral(other_top[:, None] + length[:, None] * boundaries)
            delta = np.diff(left - right, axis=1) * (bins / length[:, None, None, None])
            keep = np.mean(delta * delta, axis=(1, 2, 3)) <= cutoff * cutoff
            if not np.any(keep):
                return False
            if bins == 1 and np.count_nonzero(keep) > 128:
                return True  # 仍有很多可能位置时，及时交还原搜索，不继续花费预筛时间。
            top, length, other_top = top[keep], length[keep], other_top[keep]
        return True


class _LocalSearchData:
    """一份不再改写的灰度图；纹理条及精确摘要按需计算。"""
    def __init__(self, gray):
        self.gray = gray
        self._templates = None
        self._digest = None
        self._row_summary = None
        self._lock = threading.Lock()

    def templates(self, work):
        with self._lock:
            if self._templates is not None:
                return self._templates
            sh = len(self.gray)
            height = min(24, max(8, sh // 3))
            energy = np.mean(np.abs(np.diff(self.gray.astype(np.float32), axis=1)), axis=1)
            textured = int(np.argmax(np.convolve(energy, np.ones(height), mode='valid')))
            self._templates = [(y, self.gray[y:y + height, 2:-2])
                               for y in sorted({0, max(0, (sh - height) // 2), sh - height, textured})
                               if not float(self.gray[y:y + height, 2:-2].std()) < 3]
            return self._templates

    def digest(self):
        with self._lock:
            if self._digest is None:
                self._digest = (self.gray.shape, hashlib.sha256(self.gray.tobytes()).digest())
            return self._digest

    def rows(self, pixels, work):
        with self._lock:
            if self._row_summary is None:
                self._row_summary = _LocalRows(pixels)
            return self._row_summary


class _LocalBoundedSearch:
    """横向分块平均只用于保守限制位置；返回前仍在原尺寸搜索。

    中心化图像 a=Pa+Qa、b=Pb+Qb，P 为分块常数投影，则
    a·b <= Pa·Pb + ||Qa||||Qb||。保留仍可能通过原门槛的位置，
    低方差、临界分数、重复峰及范围过大时回退。不同页面不依靠小图合并。
    """
    def __init__(self, work):
        self.work = work
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def _prefix(values):
        return np.concatenate(([0.], np.cumsum(values, dtype=np.float64)))

    def prepare(self, data):
        key = data.digest()
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            width = data.gray.shape[1] - 4
            edges = np.linspace(0, width, 33, dtype=int)
            weights = np.sqrt(np.diff(edges))
            values = data.gray.astype(np.float32)
            parts = []
            for x in range(5):
                crop = values[:, x:x + width]
                projected = (np.add.reduceat(crop, edges[:-1], axis=1) / weights).astype(np.float32)
                parts.append((projected, self._prefix(crop.sum(axis=1, dtype=np.float64)),
                              self._prefix(np.einsum('ij,ij->i', crop, crop, dtype=np.float64)),
                              self._prefix(np.einsum('ij,ij->i', projected, projected, dtype=np.float64))))
            result = weights, parts
            self.cache[key] = result
            if len(self.cache) > 8:
                self.cache.popitem(last=False)
            return result

    def bounds(self, source, target, y, patch):
        cv = _local_cv()
        weights, source_parts = self.prepare(source)
        _, target_parts = self.prepare(target)
        height, width = patch.shape
        area = height * width
        mean = float(patch.mean())
        variance = float(np.sum((patch.astype(np.float64) - mean) ** 2))
        if variance <= 1e-8:
            return None
        projected = source_parts[2][0][y:y + height].astype(np.float64) - mean * weights
        residual = max(0., variance - float(np.sum(projected ** 2)))
        results = []
        for target_projection, sums, squares, projected_squares in target_parts:
            total = sums[height:] - sums[:-height]
            target_variance = squares[height:] - squares[:-height] - total * total / area
            projected_variance = projected_squares[height:] - projected_squares[:-height] - total * total / area
            target_residual = np.maximum(0., target_variance - projected_variance)
            cross = cv.matchTemplate(target_projection, projected.astype(np.float32), cv.TM_CCORR).ravel().astype(np.float64)
            denominator = np.sqrt(np.maximum(0., variance * target_variance))
            upper = np.divide(cross + np.sqrt(residual * target_residual), denominator,
                              out=np.ones_like(denominator), where=denominator > 1e-8)
            # 低方差会放大浮点相关计算误差，这些窗口保留为“不确定”。
            raw_error_guard = area * 65025 * np.finfo(np.float32).eps * 64
            upper[denominator < raw_error_guard / .01] = 1
            # 非常数模板与常数窗口在 OpenCV 中得分为 0。
            upper[target_variance < 1e-8] = 0
            results.append(upper)
        return np.column_stack(results)

    def propose(self, source, target, y, patch):
        if (patch.shape[1] < 256 or len(target.gray) - len(patch) < 64
                or source.gray.dtype != np.uint8 or target.gray.dtype != np.uint8
                or source.gray.shape[1] != target.gray.shape[1]
                or patch.shape[1] != source.gray.shape[1] - 4):
            return None
        upper = self.bounds(source, target, y, patch)
        if upper is None or not np.all(np.isfinite(upper)):
            return None
        threshold = self.work.config.local_template_threshold
        mask = np.any(upper >= threshold - .01, axis=1)
        if not np.any(mask):
            return (0., 0, 0)  # 仅表示达不到本次门槛，不伪称已求得原图最大分数。
        mask[1:] |= mask[:-1].copy()
        mask[:-1] |= mask[1:].copy()
        rows = np.flatnonzero(mask)
        bands = np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)
        if len(rows) > len(mask) * .45 or len(bands) > 6:
            return None
        peaks = []
        for band in bands:
            begin, end = int(band[0]), int(band[-1]) + 1
            matches, result = self.work.native_match(target.gray[begin:end + len(patch) - 1], patch)
            score, x, match_y = result
            peaks.append((score, x, match_y + begin))
            if matches.size > 1:
                peaks.append((float(np.partition(matches.ravel(), -2)[-2]), -1, -1))
        peaks.sort(key=lambda value: (-value[0], value[2], value[1]))
        result = peaks[0]
        if (abs(result[0] - threshold) < .002
                or (result[0] >= threshold and len(peaks) > 1 and result[0] - peaks[1][0] < .002)):
            return None
        return result

    def close(self):
        self.cache.clear()


class _LocalWork:
    """仅限一次局部分析的精确搜索缓存和可选线程池。"""
    def __init__(self, config):
        self.config = config
        self.peaks = OrderedDict()
        self.lock = threading.Lock()
        self.pool = None
        self.coarse = _LocalBoundedSearch(self) if config.local_bounded_search else None

    def equal(self, left, right, reference=None, crop=None):
        if not _strict_region_equal(left, right, self.config):
            return False
        return not (reference is not None and left.std() < 2
                    and (reference.std() >= 3 or crop.std() >= 3))

    def peak(self, source, target, y, patch):
        key = (source.digest(), target.digest(), y, patch.shape) if self.config.local_search_reuse else None
        if key is not None:
            with self.lock:
                if key in self.peaks:
                    self.peaks.move_to_end(key)
                    return self.peaks[key]
        result = self.coarse.propose(source, target, y, patch) if self.coarse is not None else None
        if result is None:
            _, result = self.native_match(target.gray, patch)
        if key is not None:
            with self.lock:
                self.peaks[key] = result
                self.peaks.move_to_end(key)
                if len(self.peaks) > 512:
                    self.peaks.popitem(last=False)
        return result

    def native_match(self, target, patch):
        cv = _local_cv()
        matches = cv.matchTemplate(target, patch, cv.TM_CCOEFF_NORMED)
        _, score, _, (x, match_y) = cv.minMaxLoc(matches)
        return matches, (score, x, match_y)

    def comparisons(self, tracks, crop, crop_gray, crop_data):
        """原顺序裁决。最近参考先串行，其余小批并行，整批结束才允许改写参考图。"""
        def locate(track):
            return _locate_local_content(track, crop, self.config, crop_gray, self, crop_data)
        if self.config.local_reference_prefilter:
            eligible = [track for track in tracks if track.pixels.shape[1] == crop.shape[1]]
            tracks = eligible
        if not tracks:
            return
        yield tracks[0], locate(tracks[0])
        workers = self.config.local_match_workers
        for start in range(1, len(tracks), workers):
            wave = tracks[start:start + workers]
            if workers == 1:
                results = [locate(wave[0])]
            else:
                if self.pool is None:
                    self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='local-match')
                # 等待整批任务结束；若一个失败，也在 wrapper 的 finally 中等待并清理。
                results = list(self.pool.map(locate, wave))
            yield from zip(wave, results)

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
        self.peaks.clear()
        if self.coarse is not None:
            self.coarse.close()
            self.coarse = None


class _LocalLocation(tuple):
    def __new__(cls, position, confidence, method):
        result = super().__new__(cls, (position, confidence))
        result.method = method
        return result


def _locate_local_content(track, crop, config, crop_gray=None, work=None, crop_data=None):
    """用有纹理的小条匹配位移，再严格核对整个重叠区；不靠匹配分数合并文字。"""
    cv = _local_cv()
    work = work if work is not None else _LocalWork(config)
    reference = track.pixels
    if reference.shape[1] != crop.shape[1]:
        return None
    rh, ch = len(reference), len(crop)
    if config.local_fast_alignment and rh == ch and rh >= 8:
        left, right = reference[:, 2:-2], crop[:, 2:-2]
        if work.equal(left, right, reference, crop):
            # 等高时，原位通过完整严格检查就是唯一最大重叠范围。
            # 不伪造未执行的模板搜索置信分数。
            return _LocalLocation(track.origin, 0.0, 'original_position')
    if (config.local_reference_prefilter and min(rh, ch) >= 8 and crop.shape[1] >= 12
            and reference.dtype == np.uint8 and crop.dtype == np.uint8):
        if crop_data is None:
            if crop_gray is None:
                crop_gray = cv.cvtColor(crop, cv.COLOR_RGB2GRAY)
            crop_data = _LocalSearchData(crop_gray)
        if track.row_summary is None:
            track.row_summary = _LocalRows(reference)
        crop_rows = crop_data.rows(crop, work)
        possible = track.row_summary.possibly_matches(crop_rows, config)
        if not possible:
            # scene 的灰度存在性还参与低对比可靠性判断，保持原来的准备副作用。
            if track.kind == 'scene' and track.gray is None:
                track.gray = cv.cvtColor(reference, cv.COLOR_RGB2GRAY)
            return None
    possible = {track.origin, track.origin + rh - ch}
    scores = {position: 0.0 for position in possible}
    if track.gray is None:
        track.gray = cv.cvtColor(reference, cv.COLOR_RGB2GRAY)
    rg = track.gray
    cg = crop_gray if crop_gray is not None else cv.cvtColor(crop, cv.COLOR_RGB2GRAY)
    if config.local_search_reuse:
        if track.search_data is None:
            track.search_data = _LocalSearchData(rg)
        reference_data = track.search_data
        crop_data = crop_data if crop_data is not None else _LocalSearchData(cg)
    else:
        reference_data, crop_data = _LocalSearchData(rg), _LocalSearchData(cg)
    for source_data, target_data, reverse in ((crop_data, reference_data, False), (reference_data, crop_data, True)):
        source, target = source_data.gray, target_data.gray
        sh, th = len(source), len(target)
        strip_height = min(24, max(8, sh // 3))
        if strip_height > th:
            continue
        for y, patch in source_data.templates(work):
            score, x, match_y = work.peak(source_data, target_data, y, patch)
            if score < config.local_template_threshold or abs(x - 2) > 1:
                continue
            position = track.origin + (y - match_y if reverse else match_y - y)
            for adjustment in (-1, 0, 1):
                possible.add(position + adjustment)
                scores[position + adjustment] = score
    best = None
    for position in sorted(possible):
        top, bottom = max(track.origin, position), min(track.origin + rh, position + ch)
        overlap = bottom - top
        if overlap < min(rh, ch) * .40 or overlap < 8:
            continue
        left = reference[top - track.origin:bottom - track.origin, 2:-2]
        right = crop[top - position:bottom - position, 2:-2]
        if not work.equal(left, right, reference, crop):
            continue
        value = (overlap, scores.get(position, 0), -abs(position - track.origin))
        if best is None or value > best[0]:
            best = (value, position, scores.get(position, 0))
    if best is None:
        return None
    return _LocalLocation(best[1], best[2], 'template_search')


def _extend_local_track(track, crop, position):
    """只扩展观察到的新范围；既有参考像素不被后续近似图覆盖，避免漂移。"""
    top = min(track.origin, position)
    bottom = max(track.origin + len(track.pixels), position + len(crop))
    if bottom - top > 4096:
        return False
    if top == track.origin and bottom == track.origin + len(track.pixels):
        return True
    combined = np.empty((bottom - top, track.pixels.shape[1], 3), dtype=np.uint8)
    combined[position - top:position - top + len(crop)] = crop
    combined[track.origin - top:track.origin - top + len(track.pixels)] = track.pixels
    track.origin, track.pixels = top, combined
    track.gray = None
    track.search_data = None
    track.row_summary = None
    track.color = np.median(combined, axis=(0, 1))
    return True


def _selection_anchors(candidates, limit):
    if not candidates:
        return []
    endings = sorted([c for c in candidates[1:] if c.keep_reason in {
        'task-end-explicit', 'task-end-auto', 'final-frame'}],
        key=lambda c: (c.keep_reason != 'final-frame', c.time), reverse=True)
    return [candidates[0], *endings[:max(0, limit - 1)]]


def _local_track_bins(track, config):
    if track.bins is not None:
        return track.bins
    cv = _local_cv()
    pixels = track.pixels
    edges = cv.Canny(cv.cvtColor(pixels, cv.COLOR_RGB2GRAY), 50, 150)
    base = np.median(pixels.reshape(-1, 3), axis=0)
    colored = np.max(np.abs(pixels.astype(np.float32) - base), axis=2) >= 16
    signal = (np.count_nonzero(edges, axis=1) >= 2) | (colored.mean(axis=1) >= .02)
    bins, size = [], config.local_content_bin_height
    for number in range(math.floor(track.origin / size), math.ceil((track.origin + len(pixels)) / size)):
        top, bottom = max(track.origin, number * size), min(track.origin + len(pixels), (number + 1) * size)
        rows = np.flatnonzero(signal[top - track.origin:bottom - track.origin]) + top
        if len(rows):
            bins.append((number, rows))
    return bins or [(0, np.array([track.origin + len(pixels) // 2]))]


def _local_views(candidates, config, fingerprints):
    """最多预读八张，顺序消费结果，避免同时持有整段视频的原尺寸图。"""
    from PIL import Image
    cv = _local_cv()
    identities = dict(_FEATURE_KEYS.get() or {})
    def load(candidate):
        digest = identities.get(str(candidate.path))
        payload = None
        if not isinstance(digest, bytes):
            payload = candidate.path.read_bytes()
            digest = hashlib.sha256(payload).digest()
        if digest in fingerprints:
            return candidate, digest, None, None, None, None
        with Image.open(io.BytesIO(payload) if payload is not None else candidate.path) as image:
            source = np.asarray(_masked_analysis_image(image.convert('RGB'), config.analysis_mask_box))
        height, width = source.shape[:2]
        scale = min(1.0, config.local_analysis_max_edge / max(height, width))
        small = cv.resize(source, (max(8, round(width * scale)), max(8, round(height * scale))),
                          interpolation=cv.INTER_AREA)
        boxes, background = _local_boxes(small)
        return candidate, digest, source, small, boxes, background
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='frame-local-read') as workers:
        iterator, pending = iter(candidates), deque()
        for _ in range(8):
            candidate = next(iterator, None)
            if candidate is None:
                break
            pending.append(workers.submit(load, candidate))
        while pending:
            yield pending.popleft().result()
            candidate = next(iterator, None)
            if candidate is not None:
                pending.append(workers.submit(load, candidate))


def _analyze_local_content(candidates, config):
    work = _LocalWork(config)
    try:
        return _analyze_local_content_impl(candidates, config, work)
    finally:
        work.close()



def _analyze_local_content_impl(candidates, config, work):
    """仅分析名额分配前的候选；计算过程与实验台记录完全独立。"""
    cv = _local_cv()
    analysis = _LocalAnalysis(list(candidates))
    tracks, backgrounds, fingerprints, background_keys = [], [], {}, {}
    background_descriptors = np.empty((len(candidates), 48), dtype=np.float32)
    for index, (candidate, digest, source, small, boxes, background) in enumerate(_local_views(candidates, config, fingerprints)):
        identity = id(candidate)
        analysis.coverage[identity] = set()
        if digest in fingerprints:
            previous = fingerprints[digest]
            analysis.background_ids[identity] = analysis.background_ids[id(previous)]
            analysis.quality[identity] = analysis.quality[id(previous)]
            analysis.descriptors[identity] = analysis.descriptors[id(previous)]
            for track in tracks:
                old = next((o for o in track.observations if o['candidate'] is previous), None)
                if old is not None:
                    track.observations.append({**old, 'candidate': candidate})
            background_id = analysis.background_ids[identity]
            analysis.coverage[identity].add(background_id)
            continue
        fingerprints[digest] = candidate
        height, width = source.shape[:2]
        scale = min(1.0, config.local_analysis_max_edge / max(height, width))
        fallback = not boxes
        if fallback:
            boxes = [(0, 0, small.shape[1], small.shape[0])]
        masked = small.copy()
        exposures, clipped_edges, sharpness, motion = [], [], [], []
        for x, y, w, h in boxes:
            x0, y0 = round(x / scale), round(y / scale)
            x1, y1 = min(width, round((x + w) / scale)), min(height, round((y + h) / scale))
            source_crop = source[y0:y1, x0:x1]
            # 局部卡片保留原图采样尺度。分别按可见高度缩放会改变字形，
            # 使同一卡片的半张/全张无法通过严格重叠检查。
            if fallback:
                crop = source_crop[2:-2].copy() if source_crop.shape[1] <= 320 else small[2:-2].copy()
            else:
                crop = source_crop[2:-2].copy()
            kind = 'scene' if fallback else 'local'
            ratio = w / small.shape[1]
            matched = None
            crop_color = np.median(crop, axis=(0, 1))
            crop_gray = cv.cvtColor(crop, cv.COLOR_RGB2GRAY)
            crop_data = _LocalSearchData(crop_gray)
            active = [track for track in tracks if track.pixels is not None and track.kind == kind]
            # 位移跟踪只检查时间上邻近的内容；跨时间的完全重复由文件标记复用。
            # 整页逐一匹配几十个旧页面既不能解释操作，还会使动态视频耗时失控。
            recent = sorted(active, key=lambda t: (-t.last_index, t.index))[:1 if fallback else 8]
            eligible = [track for track in recent if .85 <= ratio / track.width_ratio <= 1.18
                        and not (fallback and np.abs(crop_color - track.color).max() > 40)]
            for track, location in work.comparisons(eligible, crop, crop_gray, crop_data):
                if location is not None and _extend_local_track(track, crop, location[0]):
                    matched = track, location
                    break
            if matched is None:
                track = _LocalTrack(len(tracks), crop.copy(), 0, ratio, kind, index)
                track.color, track.gray = crop_color, crop_gray
                track.row_summary = crop_data._row_summary
                if fallback and float(crop_gray.std()) < 2:
                    track.reliable = not any(old.gray is not None and float(old.gray.std()) < 2
                                             and np.abs(old.color - crop_color).max() <= 8 for old in active)
                tracks.append(track)
                position = 0
            else:
                track, location = matched
                position = location[0]
            track.last_index = index
            track.observations.append({'candidate': candidate, 'top': position, 'bottom': position + len(crop)})
            clipped = 0 if fallback else sum((x <= 1, y <= 1, x + w >= small.shape[1] - 1,
                                              y + h >= small.shape[0] - 1))
            clarity = float(cv.Laplacian(crop_gray, cv.CV_32F).var())
            exposures.append(len(crop))
            clipped_edges.append(clipped)
            sharpness.append(clarity)
            motion.append(abs(position))
            masked[y:y + h, x:x + w] = background
        signature = cv.resize(masked, (96, 160), interpolation=cv.INTER_AREA)
        background_key = hashlib.sha256(signature.tobytes()).digest()
        background_index = background_keys.get(background_key)
        descriptor = cv.resize(signature, (4, 4), interpolation=cv.INTER_AREA).astype(np.float32).ravel() / 255
        if background_index is None and backgrounds:
            distances = np.mean((background_descriptors[:len(backgrounds)] - descriptor) ** 2, axis=1)
            possible = np.flatnonzero(distances <= (config.global_dedup_rms_threshold + 2 / 255) ** 2)
            background_index = next((int(i) for i in possible
                                     if work.equal(signature, backgrounds[i])), None)
        if background_index is None:
            background_index = len(backgrounds)
            uniform = bool(np.all(signature == signature[0, 0]))
            weak_background = uniform and any(ref.shape[:2] == (1, 1)
                                               and np.abs(ref[0, 0].astype(np.int16) - signature[0, 0]).max() <= 8
                                               for ref in backgrounds)
            backgrounds.append(signature[:1, :1].copy() if np.all(signature == signature[0, 0]) else signature)
            background_descriptors[background_index] = descriptor
            key = f'b{background_index:04d}'
            analysis.units[key] = {'id': key, 'kind': 'background', 'weight': 0.0 if weak_background else .15}
        background_keys[background_key] = background_index
        background_id = f'b{background_index:04d}'
        analysis.units.setdefault(background_id, {'id': background_id, 'kind': 'background', 'weight': .15})
        analysis.coverage[identity].add(background_id)
        analysis.quality[identity] = (sum(exposures), -sum(clipped_edges),
                                      round(sum(sharpness) / max(1, len(sharpness)), 4), -sum(motion))
        analysis.descriptors[identity] = cv.resize(small, (12, 20), interpolation=cv.INTER_AREA).astype(np.float32) / 255
        analysis.background_ids[identity] = background_id
        # 活跃跟踪最多保存 32 份参考图；更早的内容只留覆盖区间和记录。
        active = sorted((track for track in tracks if track.pixels is not None),
                        key=lambda t: (t.last_index, t.index))
        for old in active[:-32]:
            old.bins = _local_track_bins(old, config)
            old.pixels = None
            old.gray = None
            old.search_data = None
            old.row_summary = None
    for track in tracks:
        bins = _local_track_bins(track, config)
        for number, rows in bins:
            key = f'r{track.index:04d}:{number}'
            analysis.units[key] = {'id': key, 'kind': track.kind, 'track': track.index,
                                   'weight': (1.5 if track.kind == 'local' else 1.0) / len(bins) if track.reliable else 0.0,
                                   'range': [int(rows.min()), int(rows.max()) + 1]}
            for observation in track.observations:
                if float(np.mean((rows >= observation['top']) & (rows < observation['bottom']))) >= .8:
                    analysis.coverage[id(observation['candidate'])].add(key)
    anchors = {id(c) for c in _selection_anchors(candidates, config.max_frames)}
    def rank(candidate):
        coverage = analysis.coverage[id(candidate)]
        return (sum(analysis.units[key]['weight'] for key in coverage), len(coverage),
                *analysis.quality[id(candidate)], candidate.support_s, candidate.time)
    ordered = sorted(candidates, key=rank, reverse=True)
    # 固定参考的直接覆盖关系，不沿被删除图片串联合并；首尾始终保留候选资格。
    for candidate in ordered:
        identity = id(candidate)
        covering = next((rep for rep in analysis.representatives
                         if analysis.coverage[identity] <= analysis.coverage[id(rep)]), None)
        if identity in anchors or covering is None:
            analysis.representatives.append(candidate)
    analysis.representatives.sort(key=lambda c: c.time)
    return analysis

@dataclass(frozen=True)
class _ScrollSettings:
    analysis_edge: int = 800
    min_width: int = 400
    min_height: int = 800
    border_width_fraction: float = 0.025
    status_height_fraction: float = 0.04
    bottom_edge_fraction: float = 0.012
    orb_features: int = 600
    match_ratio: float = 0.75
    motion_tolerance: float = 2.0
    minimum_motion_points: int = 8
    pixel_difference: int = 28
    contrast_threshold: int = 20
    residual_component_limit: int = 12
    residual_pixel_limit: int = 90
    local_structure_correlation: float = 0.94
    analysis_mask_box: tuple = (0.63, 0.005, 0.74, 0.04)
    agent_indicator_box: tuple = (0.32, 0.955, 0.7, 0.995)

def _scroll_touch_mask(rgb, neighbors):
    """Only a filled achromatic disc absent in BOTH temporal neighbors, on blank background."""
    cv = _local_cv()
    gray = cv.cvtColor(rgb, cv.COLOR_RGB2GRAY)
    (h, w) = gray.shape
    bright = ((gray >= 155) & (_scroll_channel_range(rgb) < 14)).astype(np.uint8)
    (n, labels, stats, centers) = cv.connectedComponentsWithStats(bright, 8)
    result = np.zeros_like(gray, np.uint8)
    circles = []
    for i in range(1, n):
        (x, y, bw, bh, area) = map(int, stats[i])
        (cx, cy) = centers[i]
        if not (40 <= area <= 1800 and 0.8 <= bw / max(1, bh) <= 1.25 and (area / (bw * bh) > 0.62)):
            continue
        radius = max(bw, bh) / 2
        if not 4 <= radius <= 25:
            continue
        if x < round(0.04 * w) or x + bw > round(0.96 * w) or y < round(0.1 * h) or (y + bh > round(0.9 * h)):
            continue
        margin = round(radius * 0.9) + 4
        (ax0, ay0) = (max(0, x - margin), max(0, y - margin))
        (ax1, ay1) = (min(w, x + bw + margin), min(h, y + bh + margin))
        (yy, xx) = np.mgrid[ay0:ay1, ax0:ax1]
        disk = (xx - cx) ** 2 + (yy - cy) ** 2 <= (radius + 2) ** 2
        ring = (xx - cx) ** 2 + (yy - cy) ** 2 >= (radius + 3) ** 2
        patch = gray[ay0:ay1, ax0:ax1]
        if patch[ring].std() > 12 or patch[disk].mean() - patch[ring].mean() < 65:
            continue
        absent = []
        for other in neighbors:
            og = cv.cvtColor(other[ay0:ay1, ax0:ax1], cv.COLOR_RGB2GRAY)
            absent.append(float(og[disk].mean() - og[ring].mean()) < 25 and float(og.std()) < 16)
        if len(absent) != 2 or not all(absent):
            continue
        result[ay0:ay1, ax0:ax1][disk] = 255
        circles.append({'center': [round(float(cx), 1), round(float(cy), 1)], 'radius': round(radius, 1)})
    return (result, circles)

def _scroll_motions(a, b):
    cv = _local_cv()
    c = a.config
    if a.descriptors is None or b.descriptors is None:
        return ([], 0)
    pairs = cv.BFMatcher(cv.NORM_HAMMING).knnMatch(a.descriptors, b.descriptors, k=2)
    matches = [p[0] for p in pairs if len(p) == 2 and p[0].distance < c.match_ratio * p[1].distance]
    if not matches:
        return ([], 0)
    aa = np.float32([a.points[m.queryIdx].pt for m in matches])
    bb = np.float32([b.points[m.trainIdx].pt for m in matches])
    delta = bb - aa
    remaining = np.ones(len(delta), bool)
    models = []
    for _ in range(3):
        if remaining.sum() < c.minimum_motion_points:
            break
        options = np.flatnonzero(remaining)
        support = np.max(np.abs(delta[options, None, :] - delta[None, :, :]), axis=2) <= c.motion_tolerance
        chosen = options[int(np.argmax(support.sum(axis=1)))]
        members = np.max(np.abs(delta - delta[chosen]), axis=1) <= c.motion_tolerance
        shift = np.median(delta[members], axis=0)
        members = np.max(np.abs(delta - shift), axis=1) <= c.motion_tolerance
        remaining[members] = False
        if members.sum() < c.minimum_motion_points:
            continue
        span = np.ptp(aa[members], axis=0)
        if span.max() < 35:
            continue
        (dx, dy) = np.rint(shift / a.scale).astype(int)
        if max(abs(dx), abs(dy)) <= 3:
            continue
        if min(abs(dx), abs(dy)) > max(abs(dx), abs(dy)) * 0.15:
            continue
        models.append({'shift': [int(dx), int(dy)], 'points': int(members.sum()), 'span': span.tolist()})
    return (models, len(matches))

def _scroll_channel_range(rgb):
    cv = _local_cv()
    (r, g, b) = cv.split(rgb)
    return cv.subtract(cv.max(cv.max(r, g), b), cv.min(cv.min(r, g), b))

class _ScrollView:

    def __init__(self, engine, fid):
        cv = _local_cv()
        self.engine = engine
        self.fid = fid
        self.config = engine.config
        rgb = engine.raw(fid).copy()
        (h, w) = rgb.shape[:2]
        for box in [self.config.analysis_mask_box, self.config.agent_indicator_box]:
            if box is None:
                continue
            rgb[round(box[1] * h):round(box[3] * h), round(box[0] * w):round(box[2] * w)] = 0
        self.rgb = cv.GaussianBlur(rgb, (3, 3), 0.55)
        self.gray = cv.cvtColor(self.rgb, cv.COLOR_RGB2GRAY)
        self.scale = min(1.0, self.config.analysis_edge / max(h, w))

    def bounds(self):
        (h, w) = self.gray.shape
        c = self.config
        return (round(c.border_width_fraction * w), round(c.status_height_fraction * h), round(w * (1 - c.border_width_fraction)), round(h * (1 - c.bottom_edge_fraction)))

    def quality(self):
        if self.fid not in self.engine.qualities:
            (x0, y0, x1, y1) = self.bounds()
            self.engine.qualities[self.fid] = int(self.foreground[y0:y1, x0:x1].sum())
        return self.engine.qualities[self.fid]

    def __getattr__(self, key):
        cv = _local_cv()
        if key == 'foreground':
            value = cv.absdiff(self.gray, cv.GaussianBlur(self.gray, (0, 0), 2)) >= self.config.contrast_threshold
        elif key == 'lower':
            value = cv.erode(self.rgb, np.ones((3, 3), np.uint8))
        elif key == 'upper':
            value = cv.dilate(self.rgb, np.ones((3, 3), np.uint8))
        elif key == 'touch_mask':
            value = self.engine.touch(self.fid)
        elif key in ('points', 'descriptors'):
            if self.fid not in self.engine.features:
                small = cv.resize(self.gray, None, fx=self.scale, fy=self.scale, interpolation=cv.INTER_AREA)
                valid = np.zeros_like(small)
                (x0, y0, x1, y1) = self.bounds()
                valid[round(y0 * self.scale):round(y1 * self.scale), round(x0 * self.scale):round(x1 * self.scale)] = 255
                value = cv.ORB_create(nfeatures=self.config.orb_features, fastThreshold=12).detectAndCompute(small, valid)
                self.engine.features[self.fid] = value
                if len(self.engine.features) > 64:
                    self.engine.features.popitem(last=False)
            self.engine.features.move_to_end(self.fid)
            (self.points, self.descriptors) = self.engine.features[self.fid]
            return getattr(self, key)
        else:
            raise AttributeError(key)
        setattr(self, key, value)
        return value

class _ScrollEngine:

    def __init__(self, frames, root, view_capacity=4, raw_capacity=6, config=None):
        self.frames = frames
        self.root = root
        self.config = config if config is not None else _ScrollSettings()
        self.lookup = {f['id']: f for f in frames}
        self.indices = {f['id']: i for (i, f) in enumerate(frames)}
        self.raw_cache = OrderedDict()
        self.views = OrderedDict()
        self.features = OrderedDict()
        self.touches = OrderedDict()
        self.qualities = {}
        self.pairs = {}
        self.view_capacity = view_capacity
        self.raw_capacity = raw_capacity

    def raw(self, fid):
        from PIL import Image
        if fid not in self.raw_cache:
            with Image.open(self.root / self.lookup[fid]['image']) as im:
                self.raw_cache[fid] = np.array(im.convert('RGB'))
            if len(self.raw_cache) > self.raw_capacity:
                self.raw_cache.popitem(last=False)
        self.raw_cache.move_to_end(fid)
        return self.raw_cache[fid]

    def touch(self, fid):
        if fid not in self.touches:
            i = self.indices[fid]
            neighbors = [self.raw(self.frames[j]['id']) for j in [i - 1, i + 1] if 0 <= j < len(self.frames)]
            (mask, _) = _scroll_touch_mask(self.raw(fid), neighbors)
            self.touches[fid] = np.packbits(mask.ravel() > 0)
            if len(self.touches) > 64:
                self.touches.popitem(last=False)
        self.touches.move_to_end(fid)
        (h, w) = self.raw(fid).shape[:2]
        return np.unpackbits(self.touches[fid], count=h * w).reshape(h, w) * np.uint8(255)

    def view(self, fid):
        if fid not in self.views:
            self.views[fid] = _ScrollView(self, fid)
            if len(self.views) > self.view_capacity:
                self.views.popitem(last=False)
        self.views.move_to_end(fid)
        return self.views[fid]

    def compare(self, a, b):
        key = (a, b)
        if key not in self.pairs:
            self.pairs[key] = {'a': a, 'b': b, **_scroll_judge(self.view(a), self.view(b))}
        return self.pairs[key]


    def close(self):
        self.views.clear()
        self.raw_cache.clear()
        self.features.clear()
        self.touches.clear()
        self.qualities.clear()
        self.pairs.clear()

def _scroll_judge(a, b):
    cv = _local_cv()
    if a.rgb.shape != b.rgb.shape:
        return {'decision': 'keep', 'reason': '尺寸变化'}
    # Native-pixel tolerances were validated on phone recordings. Do not
    # erase small controls when a lower-resolution input has thinner strokes.
    height, width = a.rgb.shape[:2]
    if width < a.config.min_width or height < a.config.min_height:
        return {'decision': 'uncertain', 'reason': '候选图尺寸较小，未确认滑动重复'}
    c = a.config
    (x0, y0, x1, y1) = a.bounds()
    (h, w) = a.gray.shape
    (motion_models, match_count) = _scroll_motions(a, b)
    if match_count < 8:
        return {'decision': 'uncertain', 'reason': '缺少可靠的共同图案', 'feature_matches': match_count, 'terminated_before_native': True}
    moving = sorted(motion_models, key=lambda m: -m['points'])[:1]
    shifts = [(0, 0)] + [tuple(m['shift']) for m in moving]
    error = np.full((h, w), 255, np.uint8)
    foreground = np.zeros((h, w), bool)
    for (dx, dy) in shifts:
        (ax0, ax1) = (max(x0, x0 - dx), min(x1, x1 - dx))
        (ay0, ay1) = (max(y0, y0 - dy), min(y1, y1 - dy))
        if ax1 <= ax0 or ay1 <= ay0:
            continue
        sa = np.s_[ay0:ay1, ax0:ax1]
        sb = np.s_[ay0 + dy:ay1 + dy, ax0 + dx:ax1 + dx]
        ar = a.rgb[sa]
        br = b.rgb[sb]
        delta = cv.max(cv.max(cv.subtract(a.lower[sa], br), cv.subtract(br, a.upper[sa])), cv.max(cv.subtract(b.lower[sb], ar), cv.subtract(ar, b.upper[sb])))
        channels = cv.split(delta)
        delta = cv.max(cv.max(channels[0], channels[1]), channels[2])
        delta[(a.touch_mask[sa] > 0) | (b.touch_mask[sb] > 0)] = 0
        error[sa] = np.minimum(error[sa], delta)
        foreground[sa] |= a.foreground[sa] | b.foreground[sb]
    valid = np.zeros((h, w), bool)
    valid[y0:y1, x0:x1] = True
    if moving:
        (dx, dy) = moving[0]['shift']
        if dy > 0:
            valid[max(y0, y1 - dy):y1] = False
        elif dy < 0:
            valid[y0:min(y1, y0 - dy)] = False
        if dx > 0:
            valid[:, max(x0, x1 - dx):x1] = False
        elif dx < 0:
            valid[:, x0:min(x1, x0 - dx)] = False
    changed = ((error >= c.pixel_difference) & foreground & valid).astype(np.uint8)
    (n, labels, stats, _) = cv.connectedComponentsWithStats(changed, 8)
    initial_pixels = int(changed.sum())
    explained_components = 0
    for i in sorted(range(1, n), key=lambda i: -stats[i, 4]):
        (x, y, bw, bh, area) = map(int, stats[i])
        if area < 3 or bw > 160 or bh > 100:
            if area >= c.residual_component_limit:
                return _scroll_early_failure(match_count, moving, area)
            continue
        (ax0, ay0) = (max(x0, x - 5), max(y0, y - 5))
        (ax1, ay1) = (min(x1, x + bw + 5), min(y1, y + bh + 5))
        patch = a.gray[ay0:ay1, ax0:ax1]
        if patch.size < 25 or patch.std() < 8:
            if area >= c.residual_component_limit:
                return _scroll_early_failure(match_count, moving, area)
            continue
        explained = False
        for (dx, dy) in shifts:
            (bx0, by0, bx1, by1) = (ax0 + dx, ay0 + dy, ax1 + dx, ay1 + dy)
            if bx0 < 0 or by0 < 0 or bx1 > w or (by1 > h):
                continue
            search = b.gray[max(0, by0 - 2):min(h, by1 + 2), max(0, bx0 - 2):min(w, bx1 + 2)]
            if search.shape[0] < patch.shape[0] or search.shape[1] < patch.shape[1]:
                continue
            (_, score, _, loc) = cv.minMaxLoc(cv.matchTemplate(search, patch, cv.TM_CCOEFF_NORMED))
            if score < c.local_structure_correlation:
                continue
            xx = max(0, bx0 - 2) + loc[0]
            yy = max(0, by0 - 2) + loc[1]
            other = b.rgb[yy:yy + patch.shape[0], xx:xx + patch.shape[1]]
            original = a.rgb[ay0:ay1, ax0:ax1]
            if np.max(np.abs(original.mean(axis=(0, 1)) - other.mean(axis=(0, 1)))) > 18:
                continue
            explained = True
            break
        if explained:
            region = changed[y:y + bh, x:x + bw]
            region[labels[y:y + bh, x:x + bw] == i] = 0
            explained_components += 1
        if not explained and area >= c.residual_component_limit:
            return _scroll_early_failure(match_count, moving, area)
    (n, _, stats, _) = cv.connectedComponentsWithStats(changed, 8)
    largest = int(stats[1:n, cv.CC_STAT_AREA].max()) if n > 1 else 0
    pixels = int(changed.sum())
    detail = {'motion_models': moving, 'feature_matches': match_count, 'residual_pixels': pixels, 'largest_residual': largest, 'foreground_a': a.quality(), 'foreground_b': b.quality(), 'raw_residual_pixels': initial_pixels, 'structure_matched_components': explained_components, 'largest_boxes': [list(map(int, s)) for s in sorted(stats[1:], key=lambda s: -s[4])[:5]]}
    if match_count < 8:
        return {**detail, 'decision': 'uncertain', 'reason': '缺少可靠的共同图案'}
    if largest >= c.residual_component_limit or pixels > c.residual_pixel_limit:
        return {**detail, 'decision': 'keep', 'reason': '对齐后仍有前景变化'}
    return {**detail, 'decision': 'merge', 'reason': '主要图案对应，剩余变化低于试验门槛', 'representative': 'b' if b.quality() > a.quality() else 'a'}

def _scroll_early_failure(match_count, moving, area):
    return {'decision': 'keep' if match_count >= 8 else 'uncertain', 'reason': '对齐后仍有前景变化' if match_count >= 8 else '缺少可靠的共同图案', 'motion_models': moving, 'feature_matches': match_count, 'certified_residual_lower_bound': int(area), 'terminated_early': True}

class _SelectionScorer:
    """只复用已有内容记录计分；不读图、不做替换复审。"""
    def __init__(self, analysis, coverage):
        self.analysis = analysis
        self.coverage = coverage
        self.weights = {key: unit['weight'] for key, unit in analysis.units.items()}
        self.foreground = {key for key, unit in analysis.units.items()
                           if unit.get('kind') != 'background' and unit['weight'] > 0}
        self.denominators = {}

    def total(self, keys):
        return round(math.fsum(self.weights[key] for key in sorted(keys)), 12)

    def gains(self, candidate, covered):
        identity = id(candidate)
        visible = self.coverage[identity]
        foreground = visible & self.foreground
        cached = self.denominators.get(identity)
        if cached is None or cached[0] != foreground:
            cached = (foreground, self.total(foreground))
            self.denominators[identity] = cached
        denominator = cached[1]
        raw_gain = self.total(visible - covered)
        foreground_gain = self.total(foreground - covered)
        ratio = round(foreground_gain / denominator, 12) if denominator > 1e-9 else 0.0
        return raw_gain, ratio

    def rank(self, candidate, ratio, novelty, order):
        return (ratio, *self.analysis.quality[id(candidate)], novelty,
                candidate.support_s, candidate.time, order[id(candidate)])


def _choose_with_scroll(candidates, analysis, anchors, frame_ids, compare=None, *, limit=15, max_related=3,
                        max_checks=None):
    order = {id(c): i for (i, c) in enumerate(candidates)}
    lookup = {id(c): c for c in candidates}
    selected = list(anchors[:limit])
    protected = {id(c) for c in selected}
    groups = {id(c): [id(c)] for c in selected}
    coverage = {key: set(value) for (key, value) in analysis.coverage.items()}
    weights = {key: unit['weight'] for (key, unit) in analysis.units.items()}
    scorer = _SelectionScorer(analysis, coverage)
    blocked = set()
    checks = 0
    max_checks = 3 * len(candidates) if max_checks is None else max_checks

    def total(keys):
        return round(math.fsum((weights[k] for k in sorted(keys))), 12)

    def covered():
        return set().union(*(coverage[id(c)] for c in selected)) if selected else set()

    def related(candidate, rep):
        (ca, cb) = (coverage[id(candidate)], coverage[id(rep)])
        shared = total((k for k in ca & cb if analysis.units[k]['kind'] != 'background'))
        distance = float(np.mean((analysis.descriptors[id(candidate)] - analysis.descriptors[id(rep)]) ** 2))
        return (shared, distance)

    def _scroll_judge(rep, candidate):
        nonlocal checks
        if checks >= max_checks:
            return None
        checks += 1
        return compare(frame_ids[id(rep)], frame_ids[id(candidate)])
    while len(selected) < limit:
        present = {id(c) for c in selected}
        seen = covered()
        remaining = [c for c in analysis.representatives if id(c) not in present | blocked]
        gains = {id(c): scorer.gains(c, seen) for c in remaining}
        useful = [c for c in remaining if gains[id(c)][0] > 1e-09]
        if not useful:
            break

        def rank(candidate):
            ident = id(candidate)
            novelty = min((float(np.mean((analysis.descriptors[ident] - analysis.descriptors[id(c)]) ** 2)) for c in selected), default=1.0)
            return scorer.rank(candidate, gains[ident][1], novelty, order)
        ranks = {id(c): rank(c) for c in useful}
        candidate = max(useful, key=lambda c: ranks[id(c)])
        cid = id(candidate)
        matched = False
        if compare is not None and checks < max_checks:
            possible = []
            for rep in selected:
                (shared, distance) = related(candidate, rep)
                if shared > 0 or distance <= 0.04:
                    possible.append((rep, shared, distance))
            possible.sort(key=lambda item: (-item[1], item[2], abs(item[0].time - candidate.time), order[id(item[0])]))
            before = sorted((p for p in possible if p[0].time <= candidate.time), key=lambda p: (candidate.time - p[0].time, order[id(p[0])]))
            after = sorted((p for p in possible if p[0].time > candidate.time), key=lambda p: (p[0].time - candidate.time, order[id(p[0])]))
            priority = before[:1] + after[:1] + possible
            unique = []
            used = set()
            for p in priority:
                if id(p[0]) not in used:
                    used.add(id(p[0]))
                    unique.append(p)
            possible = unique
            for rep, _, _ in possible[:max_related]:
                result = _scroll_judge(rep, candidate)
                if result is None:
                    break
                rid = id(rep)
                if result['decision'] != 'merge':
                    continue
                winner = cid if result['representative'] == 'b' else rid
                if winner == cid and rid in protected:
                    continue
                if winner == cid:
                    verified = True
                    for member in groups[rid]:
                        if member == rid:
                            continue
                        check = _scroll_judge(lookup[member], candidate)
                        if check is None or check['decision'] != 'merge':
                            verified = False
                            break
                    if not verified:
                        continue
                    selected[selected.index(rep)] = candidate
                    groups[cid] = groups.pop(rid) + [cid]
                    blocked.add(rid)
                    coverage[cid] |= coverage[rid]
                else:
                    blocked.add(cid)
                    groups[rid].append(cid)
                    coverage[rid] |= coverage[cid]
                matched = True
                break
        if not matched:
            selected.append(candidate)
            groups[cid] = [cid]
    selected.sort(key=lambda c: (c.time, order[id(c)]))
    return selected


def _limit_candidates(candidates, limit, *, config, analysis):
    """Select from the original coverage model, checking scroll duplicates before adding a frame."""
    pool = analysis.candidates
    frame_ids = {id(candidate): f"c{index:05d}" for index, candidate in enumerate(pool)}
    frames = [{"id": frame_ids[id(candidate)], "image": candidate.path} for candidate in pool]
    settings = _ScrollSettings(
        analysis_edge=config.scroll_analysis_max_edge,
        min_width=config.scroll_min_width,
        min_height=config.scroll_min_height,
        analysis_mask_box=config.analysis_mask_box,
        agent_indicator_box=config.scroll_agent_indicator_box,
    )
    engine = (_ScrollEngine(frames, Path(), view_capacity=16, raw_capacity=48, config=settings)
              if config.scroll_dedup_enabled else None)
    try:
        return _choose_with_scroll(
            pool, analysis, _selection_anchors(candidates, limit), frame_ids,
            engine.compare if engine is not None else None, limit=limit,
            max_related=config.scroll_max_related,
            max_checks=config.scroll_checks_per_candidate * len(pool),
        )
    finally:
        if engine is not None:
            engine.close()


@_with_feature_cache
def _hybrid_keyframes(
    video: Path,
    work_dir: Path,
    config: KeyframeConfig,
    duration: float,
) -> tuple[list[_Candidate], float | None, float | None]:
    effective_task_end, raw = _extract_candidates(
        video,
        work_dir,
        config,
        duration,
    )
    _prewarm_features(raw, _content_signature)
    terminal_candidates = [
        candidate
        for candidate in raw
        if candidate.source.startswith("terminal")
    ]
    algorithm_candidates = [
        candidate
        for candidate in raw
        if not candidate.source.startswith("terminal")
    ]
    all_timestamp_unique = _canonicalize_timestamps(
        algorithm_candidates,
        config.timestamp_merge_gap_s,
        config=config,
    )

    task_end_candidate: _Candidate | None = None
    task_end_confidence: float | None = None
    if effective_task_end is not None:
        task_end_candidate = next(
            (
                candidate
                for candidate in all_timestamp_unique
                if candidate.source == "task-end"
            ),
            None,
        )
    else:
        task_end_candidate, task_end_confidence = _infer_task_end_candidate(
            all_timestamp_unique,
            terminal_candidates[-1] if terminal_candidates else None,
            config,
        )
        if task_end_candidate is not None:
            effective_task_end = task_end_candidate.time

    timestamp_unique = (
        [
            candidate
            for candidate in all_timestamp_unique
            if candidate.time <= effective_task_end + 0.001
        ]
        if effective_task_end is not None
        else all_timestamp_unique
    )
    state_unique = _deduplicate_states(timestamp_unique, config)

    forced: list[_Candidate] = []
    if task_end_candidate is not None:
        task_end_candidate.keep_reason = (
            "task-end-explicit"
            if config.task_end_time is not None
            else "task-end-auto"
        )
        forced.append(task_end_candidate)
    if terminal_candidates:
        terminal = terminal_candidates[-1]
        terminal.keep_reason = "final-frame"
        forced.append(terminal)
        state_unique = [
            candidate
            for candidate in state_unique
            if candidate is task_end_candidate
            or abs(candidate.time - terminal.time) > 0.5
        ]

    combined = {
        id(candidate): candidate for candidate in state_unique + forced
    }
    combined_ordered = sorted(combined.values(), key=lambda candidate: candidate.time)
    content_unique = _merge_repeated_content(combined_ordered, config)
    analysis = _analyze_local_content(content_unique, config)
    limited = _limit_candidates(analysis.representatives, config.max_frames, config=config, analysis=analysis)
    return limited, effective_task_end, task_end_confidence


def extract_scene_keyframes(
    video: Path | str,
    out_dir: Path | str,
    max_frames: int = 15,
    min_frames: int = 4,
    threshold: float = 0.02,
    min_gap_s: float = 0.15,
    max_edge: int = 720,
    *,
    task_start_time: float = DEFAULT_TASK_START_TIME,
    task_end_time: float | None = None,
    sample_fps: float = 3.0,
    config: KeyframeConfig | None = None,
    algorithm_version: str = KEYFRAME_ALGORITHM_VERSION,
) -> list[Path]:
    """抽取操作录屏关键帧并返回有序图片路径。

    旧参数 ``max_frames/min_frames/threshold/min_gap_s/max_edge`` 保持兼容；
    ``min_frames`` 在新算法中不再强制均匀补帧。高级参数可通过
    :class:`KeyframeConfig` 一次性传入。
    """
    del min_frames
    video = Path(video)
    out_dir = Path(out_dir)
    if config is None:
        config = KeyframeConfig(
            task_start_time=task_start_time,
            task_end_time=task_end_time,
            max_frames=max_frames,
            sample_fps=sample_fps,
            scene_threshold=threshold,
            scene_min_gap_s=min_gap_s,
            max_edge=max_edge,
        )
    elif (
        task_start_time != DEFAULT_TASK_START_TIME
        or task_end_time is not None
    ):
        raise ValueError("传入 config 时不能再单独传任务起止时间")
    duration = probe_duration(video)
    if duration <= 0:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("kf_*.jpg"):
        stale.unlink(missing_ok=True)
    metadata_path = out_dir / "keyframes.json"
    metadata_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="ae_kf_candidates_") as temp_dir:
        selected, effective_task_end, confidence = _hybrid_keyframes(
            video,
            Path(temp_dir),
            config,
            duration,
        )
        frames: list[Path] = []
        records: list[dict] = []
        for index, candidate in enumerate(selected, start=1):
            target = out_dir / f"kf_{index:03d}.jpg"
            shutil.copy2(candidate.path, target)
            frames.append(target)
            records.append(
                {
                    "index": index,
                    "time": round(candidate.time, 3),
                    "source": candidate.source,
                    "keep_reason": candidate.keep_reason,
                }
            )

        metadata = {
            "algorithm_version": algorithm_version,
            "video": str(video),
            "duration": round(duration, 3),
            "config": asdict(config),
            "effective_task_end_time": (
                round(effective_task_end, 3)
                if effective_task_end is not None
                else None
            ),
            "task_end_confidence": (
                round(confidence, 5) if confidence is not None else None
            ),
            "selected_count": len(records),
            "selected": records,

        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return frames
