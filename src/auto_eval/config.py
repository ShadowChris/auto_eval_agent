"""配置加载：从 config/*.yaml 读取并校验为强类型配置对象。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class JudgeConfig(BaseModel):
    """裁判配置。"""

    name: str
    display: str | None = None  # 前端显示名（如中文"终端用户"），缺省回落 name
    runner: str = "openai_compat"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    persona: str | None = None  # end_user | ...
    temperature: float = 0.0
    top_p: float | None = None  # None=不发送，避免不支持该参数的网关 400
    seed: int | None = None  # None=不发送；同 seed 可复现但不降跨 seed 方差
    concurrency: int = 4
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    total_timeout_s: float = 180.0
    max_attempts: int = 5
    retry_base_s: float = 1.0
    retry_max_s: float = 20.0
    stream_include_usage: bool = True

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class VisualExtractionConfig(BaseModel):
    """视频视觉评估模式的抽帧与图片编码参数。"""

    algorithm_version: str
    default_start_time: float = 0.0
    max_frames: int = 20
    sample_fps: float = 1.5
    scene_threshold: float = 0.03
    scene_min_gap_s: float = 0.5
    state_layout_threshold: float = 0.025
    stable_min_duration_s: float = 0.8
    max_edge: int = 1280
    jpeg_quality: int = 85


class VisualModeProfile(BaseModel):
    """独立于垂域分类的视频视觉评估配置。"""

    name: str = ""
    display: str = ""
    card_types: dict[str, str] = Field(default_factory=dict)
    category_display: dict[str, str] = Field(default_factory=dict)  # category → 中文垂域名（未命中显示原始 category）
    extraction: VisualExtractionConfig


class AppConfig(BaseModel):
    judges: list[JudgeConfig]
    visual_modes: dict[str, VisualModeProfile] = Field(default_factory=dict)


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_visual_modes(config_dir):
    profiles_dir = Path(config_dir) / "visual_modes"
    if not profiles_dir.is_dir():
        return {}
    profiles = {}
    for f in sorted(profiles_dir.glob("*.yaml")):
        data = dict(_read_yaml(f) or {})
        name = data.pop("name", f.stem)
        profiles[name] = VisualModeProfile(name=name, **data)
    return profiles


def load_config(config_dir: str | Path) -> AppConfig:
    """读取 config_dir 下的 judges.yaml 与 visual_modes/ 子目录。"""
    config_dir = Path(config_dir)
    judges_data = _read_yaml(config_dir / "judges.yaml") or {}

    judges = [JudgeConfig(**j) for j in (judges_data.get("judges") or [])]
    visual_modes = _load_visual_modes(config_dir)
    return AppConfig(judges=judges, visual_modes=visual_modes)
