"""操作类录屏的媒体处理：场景检测抽关键帧 + 帧编码。

针对操作录屏「长静止 + 突发跳变」的特点：只在画面显著变化时抽帧，
跳过静止段、合并相邻突变、强制保留终态帧，避免等时间间隔抽帧的浪费与漏帧。

纯 ffmpeg + PIL（不依赖 cv2）。裁判看视频 = 这里抽帧 → encode_frame 成 base64 →
以 image_url 多图喂裁判（kimi-for-coding-openai 经代理支持 image_url）。
"""
from __future__ import annotations

import base64
import io
import re
import subprocess
import tempfile
from pathlib import Path


def probe_duration(video: Path | str) -> float:
    """ffprobe 取时长（秒），失败回退 0.0。"""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        )
        return float(out.strip())
    except Exception:
        return 0.0


def scene_change_times(video: Path | str, threshold: float = 0.10) -> list[float]:
    """跑一次 ffmpeg showinfo，返回画面场景变化（scene>threshold）的时间点（秒）。
    select 在前、showinfo 在后，stderr 只输出被选中的帧。失败返回 []。
    注意：Windows 默认用 GBK 解码 ffmpeg 输出会因非 GBK 字节失败（stderr 变 None），
    故显式 encoding=utf-8/errors=replace。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(video), "-an",
             "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        return []
    err = proc.stderr or ""
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", err)]


def _dedup(times: list[float], min_gap_s: float) -> list[float]:
    """相邻间隔 < min_gap_s 的合并取先者（同一次操作的连续跳变算一帧）。"""
    out: list[float] = []
    for t in times:
        if not out or t - out[-1] >= min_gap_s:
            out.append(t)
    return out


def _even_sample(times: list[float], n: int) -> list[float]:
    """从有序时间点里近似均匀取 n 个（保留首尾代表性）。"""
    if len(times) <= n:
        return list(times)
    step = len(times) / n
    return [times[int(i * step)] for i in range(n)]


def select_keyframe_times(
    video: Path | str,
    max_frames: int = 10,
    min_frames: int = 4,
    threshold: float = 0.10,
    min_gap_s: float = 0.8,
) -> list[float]:
    """决定要抽哪些时间点（秒）：场景检测 + 去重 + 上限/保底 + 终态保底。

    返回有序时间点列表。这是抽帧策略的核心，单独暴露便于单测。
    """
    dur = probe_duration(video)
    times = _dedup(scene_change_times(video, threshold), min_gap_s)

    # 上限：场景点过多（操作密集）→ 均匀抽样到 max_frames
    if len(times) > max_frames:
        times = _even_sample(times, max_frames)

    # 保底：场景点不足（画面太平静）→ 用均匀分布的点补到 min_frames
    if len(times) < min_frames and dur > 0:
        candidates = [dur * (i + 0.5) / min_frames for i in range(min_frames)]
        for c in candidates:
            if len(times) >= min_frames:
                break
            if all(abs(c - t) > min_gap_s * 0.5 for t in times):
                times.append(c)
        times.sort()

    # 终态保底：强制纳入最后一刻——「成没成」看结果态，等间隔常因时长不整除漏掉真正的结尾
    if dur > 0:
        last = max(0.0, dur - 0.1)
        if not any(abs(t - last) < 0.5 for t in times):
            times.append(last)
        times.sort()
    return times


def extract_scene_keyframes(
    video: Path | str,
    out_dir: Path | str,
    max_frames: int = 10,
    min_frames: int = 4,
    threshold: float = 0.10,
    min_gap_s: float = 0.8,
    max_edge: int = 720,
) -> list[Path]:
    """按 select_keyframe_times 的时间点逐帧精确抽图到 out_dir，返回帧路径（按时间顺序）。

    用 `-ss <t> -i video -frames:v 1` 精确 seek（比 fps 滤镜更可控、不漏指定时刻）。
    """
    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("kf_*.jpg"):
        old.unlink()
    times = select_keyframe_times(video, max_frames, min_frames, threshold, min_gap_s)
    frames: list[Path] = []
    for i, t in enumerate(times, 1):
        fp = out_dir / f"kf_{i:03d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale='min({max_edge},iw)':-2", "-q:v", "3", str(fp)],
            check=False, capture_output=True,
        )
        if fp.exists():
            frames.append(fp)
    return frames


def encode_frame(path: Path | str, max_edge: int = 768, quality: int = 70) -> str:
    """PIL 等比缩放到最长边 max_edge，JPEG 压缩，返回 data:image/jpeg;base64,... URL。"""
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = max_edge / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


def video_to_frame_urls(
    video: Path | str,
    out_dir: Path | str | None = None,
    max_frames: int = 10,
    min_frames: int = 4,
    threshold: float = 0.10,
    min_gap_s: float = 0.8,
    max_edge: int = 768,
    quality: int = 70,
) -> list[str]:
    """抽帧 + 编码一步到位，返回 base64 data_url 列表（裁判直接用作 image_url）。"""
    video = Path(video)
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="ae_kf_"))
    frames = extract_scene_keyframes(video, out_dir, max_frames, min_frames, threshold, min_gap_s)
    return [encode_frame(f, max_edge, quality) for f in frames]
