"""验证 Pip 内置 FFmpeg 能完成项目实际的视频探测与抽帧链路。"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

from auto_eval.media import KeyframeConfig, extract_scene_keyframes, probe_duration


def main() -> None:
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not ffmpeg.is_file():
        raise RuntimeError(f"Pip FFmpeg 可执行文件不存在：{ffmpeg}")

    # 强制本次验证使用 Wheel 内置二进制，同时验证没有 ffprobe 也能读取时长。
    os.environ["AUTO_EVAL_FFMPEG"] = str(ffmpeg)

    with tempfile.TemporaryDirectory(prefix="auto_eval_ffmpeg_") as temp_dir:
        root = Path(temp_dir)
        video = root / "smoke.mp4"
        frame_dir = root / "frames"
        subprocess.run(
            [
                str(ffmpeg),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=160x240:rate=10",
                "-c:v",
                "mpeg4",
                "-q:v",
                "3",
                str(video),
            ],
            check=True,
        )
        duration = probe_duration(video)
        frames = extract_scene_keyframes(
            video,
            frame_dir,
            config=KeyframeConfig(
                task_start_time=0,
                task_end_time=1.8,
                max_frames=6,
                sample_fps=1,
                max_edge=160,
                protected_sample_interval=0,
            ),
        )

        if not 1.5 <= duration <= 2.5:
            raise RuntimeError(f"视频时长读取异常：{duration}")
        if not frames or not all(frame.is_file() for frame in frames):
            raise RuntimeError("关键帧抽取失败")

    print(f"[OK] Pip FFmpeg：{ffmpeg}")
    print(f"[OK] 视频时长：{duration:.2f} 秒")
    print(f"[OK] 项目抽帧：{len(frames)} 帧")
    print("FFmpeg 环境与系统抽帧功能验证通过。")


if __name__ == "__main__":
    main()
