"""任务类版本清单、受信抽帧入口及新任务默认设置。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import date
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from pydantic import BaseModel, ConfigDict
import yaml

from .. import media, operation_keyframes
from ..paths import PROJECT_ROOT, RUNS_DIR


BUNDLE_ROOT = PROJECT_ROOT / "config" / "operation_versions"
DEFAULTS_PATH = RUNS_DIR / "settings" / "operation_versions.json"
DEFAULT_KEYFRAME_VERSION = operation_keyframes.KEYFRAME_ALGORITHM_VERSION
LEGACY_KEYFRAME_VERSION = media.KEYFRAME_ALGORITHM_VERSION
DEFAULT_STANDARD_VERSION = "operation-v1.0"
KeyframeConfig = media.KeyframeConfig | operation_keyframes.KeyframeConfig


class VersionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    updated_at: date
    previous: str | None
    changes: list[str]
    policy_hash: str | None = None
    engine: str | None = None
    params_hash: str | None = None


class OperationVersionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyframe_version: str = DEFAULT_KEYFRAME_VERSION
    standard_version: str = DEFAULT_STANDARD_VERSION


@dataclass(frozen=True)
class KeyframeVersion:
    id: str
    extract: Callable[..., list[Path]]
    params: KeyframeConfig

    def config(self, **overrides: float | None) -> KeyframeConfig:
        return replace(self.params, **overrides) if overrides else self.params

    def extract_configured(
        self, video: Path, directory: Path, config: KeyframeConfig | None = None,
    ) -> list[Path]:
        return self.extract(video, directory, config=config if config is not None else self.params,
                            algorithm_version=self.id)


# 只允许经代码审查的入口，不从 YAML/Web 导入任意 Python 模块。
KEYFRAME_ENGINES = {
    DEFAULT_KEYFRAME_VERSION: (operation_keyframes.extract_scene_keyframes, operation_keyframes.KeyframeConfig),
    LEGACY_KEYFRAME_VERSION: (media.extract_scene_keyframes, media.KeyframeConfig),
}


@lru_cache(maxsize=2)
def _manifests(kind: str) -> dict[str, tuple[VersionManifest, Path]]:
    entries = {}
    for path in sorted((BUNDLE_ROOT / kind).glob("*/manifest.yaml")):
        manifest = VersionManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if manifest.id in entries:
            raise ValueError(f"重复的任务类版本 ID：{manifest.id}")
        entries[manifest.id] = (manifest, path.parent)
    for manifest, _ in entries.values():
        if manifest.previous is not None and manifest.previous not in entries:
            raise ValueError(f"{manifest.id} 的上一版本不存在：{manifest.previous}")
        if kind == "keyframes" and (manifest.engine not in KEYFRAME_ENGINES or not manifest.params_hash):
            raise ValueError(f"{manifest.id} 缺少受信抽帧入口或参数校验值")
    return entries


def get_keyframe_version(version: str = DEFAULT_KEYFRAME_VERSION) -> KeyframeVersion:
    if not isinstance(version, str) or not is_registered_keyframe_version(version):
        raise ValueError(f"未知抽帧版本：{version}")
    return _keyframe_version(version)


def is_registered_keyframe_version(version: str | None) -> bool:
    return version in _manifests("keyframes")


@lru_cache(maxsize=32)
def _keyframe_version(version: str) -> KeyframeVersion:
    manifest, directory = _manifests("keyframes")[version]
    extract, config_type = KEYFRAME_ENGINES[manifest.engine]
    params = yaml.safe_load((directory / "params.yaml").read_text(encoding="utf-8"))
    required = {field.name for field in fields(config_type) if field.init}
    if not isinstance(params, dict) or params.keys() != required:
        raise ValueError(f"抽帧版本 {version} 必须保存全部且仅保存可配置参数")
    if _content_digest(params) != manifest.params_hash:
        raise ValueError(f"抽帧版本 {version} 的参数校验失败")
    return KeyframeVersion(version, extract, config_type(**params))


def _content_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
    ).hexdigest()


@lru_cache(maxsize=32)
def _standard_snapshot(version: str) -> dict:
    entry = _manifests("standards").get(version)
    if entry is None:
        raise ValueError(f"未知任务类标准版本：{version}")
    manifest, directory = entry
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    recorded = snapshot.pop("policy_hash", None)
    digest = _content_digest(snapshot)
    if digest != manifest.policy_hash or digest != recorded:
        raise ValueError(f"任务类标准包 {version} 的快照校验失败")
    return {**snapshot, "policy_hash": digest}


def standard_snapshot(version: str) -> dict:
    if not isinstance(version, str):
        raise ValueError("任务类标准版本必须是字符串")
    return deepcopy(_standard_snapshot(version))


def current_version_defaults() -> OperationVersionDefaults:
    if not DEFAULTS_PATH.exists():
        return OperationVersionDefaults()
    return OperationVersionDefaults.model_validate_json(DEFAULTS_PATH.read_text(encoding="utf-8"))


def pin_operation_versions(options: dict) -> dict:
    """仅在提交新任务时解析默认值及快照，不信任客户端传来的快照。"""
    defaults = current_version_defaults()
    pinned = {key: value for key, value in options.items()
              if key not in {"evaluation_snapshot", "require_keyframe_version", "reused_keyframe_versions"}}
    if pinned.get("execution_mode") == "evaluate_only":
        if "keyframe_version" in pinned:
            raise ValueError("仅评估沿用实际归档帧，不能选择抽帧算法")
    else:
        pinned["require_keyframe_version"] = "keyframe_version" in pinned
        version = get_keyframe_version(pinned.get("keyframe_version", defaults.keyframe_version))
        pinned["keyframe_version"] = version.id
    if pinned.get("execution_mode") == "extract_only":
        pinned.pop("standard_version", None)
    else:
        version = pinned.get("standard_version", defaults.standard_version)
        pinned["evaluation_snapshot"] = standard_snapshot(version)
        pinned["standard_version"] = version
    return pinned


def save_version_defaults(defaults: OperationVersionDefaults) -> None:
    get_keyframe_version(defaults.keyframe_version)
    standard_snapshot(defaults.standard_version)
    DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DEFAULTS_PATH.parent, delete=False) as handle:
            name = handle.name
            handle.write(defaults.model_dump_json(indent=2))
        os.replace(name, DEFAULTS_PATH)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


def list_versions() -> dict:
    return {
        "defaults": current_version_defaults().model_dump(),
        **{kind: [manifest.model_dump(mode="json", exclude_none=True)
                  for manifest, _ in reversed(list(_manifests(kind).values()))]
           for kind in ("keyframes", "standards")},
    }


def standard_details(version: str) -> dict:
    snapshot = standard_snapshot(version)
    manifest, _ = _manifests("standards")[version]
    skill = snapshot["operation_skill"]
    return {
        **manifest.model_dump(mode="json"),
        "policy": skill["operation_policy"],
        "rubrics": skill.get("rubrics") or snapshot["rubrics"],
        "skill_rules": skill.get("rules") or "",
        "expert_knowledge": snapshot.get("expert_knowledge"),
        "templates": snapshot["templates"],
    }
