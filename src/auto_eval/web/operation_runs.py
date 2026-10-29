"""任务类执行模式、不可变帧归档和评估配置快照。"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from ..config import AppConfig, DomainSkill, EnsembleConfig, ExpertKnowledgeBase, RubricDim
from ..judges import prompts
from ..paths import RUNS_DIR


EXECUTION_MODES = {"full", "extract_only", "evaluate_only"}
FRAME_FIELDS = {
    "frames", "frame_count", "frame_set_id", "frame_manifest_path", "image_input",
    "prepare_error", "video_prepare_warnings", "_remote_video_identity",
}


def frame_items(item: dict) -> list[dict]:
    if item.get("group_variants"):
        return [variant["item"] for variant in item["group_variants"] if isinstance(variant.get("item"), dict)]
    return [item]


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _stream_digest(handle)


def _stream_digest(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_frame(raw: str, runs_dir: Path) -> Path:
    path = Path(raw).resolve()
    if not path.is_relative_to(runs_dir.resolve()) or not path.is_file():
        raise ValueError("抽帧文件缺失或不在运行目录中，请重新抽帧创建新任务")
    return path


def verify_frame_item(item: dict, runs_dir: Path = RUNS_DIR) -> None:
    identity = str(item.get("frame_set_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("缺少受保护的抽帧版本；请从历史任务复用抽帧新建评估")
    directory = runs_dir / "frame_sets" / identity
    manifest_path = directory / "manifest.json"
    if directory.is_symlink() or manifest_path.is_symlink():
        raise ValueError("抽帧版本路径无效")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest["frames"]
        if not records or hashlib.sha256(_json_bytes(manifest)).hexdigest() != identity:
            raise ValueError("抽帧版本清单校验失败")
        if manifest.get("schema_version") == 2:
            metadata_path = directory / "keyframes.json"
            if metadata_path.is_symlink() or json.loads(metadata_path.read_text(encoding="utf-8")) != manifest["metadata"]:
                raise ValueError("抽帧元数据发生变化，请重新抽帧创建新任务")
        expected = [directory / f"kf_{index + 1:03d}.jpg" for index in range(len(records))]
        if [str(path) for path in expected] != item.get("frames"):
            raise ValueError("抽帧列表与版本清单不一致")
        for path, record in zip(expected, records):
            if path.is_symlink() or _digest(_safe_frame(str(path), runs_dir)) != record["sha256"]:
                raise ValueError("抽帧内容发生变化，请重新抽帧创建新任务")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("抽帧版本缺失或损坏；仅评估模式不会自动重新抽帧") from exc


def freeze_frame_item(item: dict, runs_dir: Path = RUNS_DIR, generation_id: str = "") -> None:
    if item.get("frame_set_id"):
        verify_frame_item(item, runs_dir)
        return
    paths = [_safe_frame(str(raw), runs_dir) for raw in item.get("frames") or []]
    if not paths:
        raise ValueError("没有可复用的抽帧；请先执行只抽帧或全流程")
    metadata = {}
    source_metadata = paths[0].parent / "keyframes.json"
    if source_metadata.is_file() and not source_metadata.is_symlink() and source_metadata.stat().st_size <= 2 * 1024 * 1024:
        metadata = json.loads(source_metadata.read_text(encoding="utf-8"))
    by_index = {str(row.get("index")): row for row in metadata.get("selected", [])}
    records = []
    selected = []
    for index, path in enumerate(paths):
        match = re.fullmatch(r"kf_(\d+)", path.stem)
        original_index = str(int(match[1])) if match else str(index + 1)
        records.append({"sha256": _digest(path), "time": by_index.get(original_index, {}).get("time")})
        selected.append({**by_index.get(original_index, {}), "index": index + 1, "time": records[-1]["time"]})
    marker = paths[0].parent / ".complete"
    extraction = {}
    if marker.is_file() and not marker.is_symlink() and marker.stat().st_size <= 2 * 1024 * 1024:
        extraction = json.loads(marker.read_text(encoding="utf-8"))
    archived_metadata = {key: deepcopy(value) for key, value in metadata.items() if key != "video"}
    archived_metadata.update({"selected": selected, "selected_count": len(selected)})
    manifest = {
        "schema_version": 2, "frames": records, "extraction": extraction,
        "generation_id": generation_id, "metadata": archived_metadata,
    }
    identity = hashlib.sha256(_json_bytes(manifest)).hexdigest()
    root = runs_dir / "frame_sets"
    root.mkdir(parents=True, exist_ok=True)
    target = root / identity
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".building-", dir=root) as temp:
            staging = Path(temp) / "frames"
            staging.mkdir()
            for index, path in enumerate(paths):
                shutil.copyfile(path, staging / f"kf_{index + 1:03d}.jpg")
            (staging / "manifest.json").write_bytes(_json_bytes(manifest))
            # 归档中不存原 URL 或源任务路径，所有引用指向本版本自己的帧。
            (staging / "keyframes.json").write_bytes(_json_bytes(archived_metadata))
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    updated = {**item, "frame_set_id": identity, "frames": [str(target / f"kf_{i + 1:03d}.jpg") for i in range(len(paths))]}
    verify_frame_item(updated, runs_dir)
    item.update(updated)
    item["frame_count"] = len(paths)
    item["frame_manifest_path"] = str(target / "manifest.json")


def freeze_frames(item: dict, *, strict: bool = False, runs_dir: Path = RUNS_DIR, generation_id: str = "") -> None:
    for row in frame_items(item):
        if strict or (row.get("frames") and not row.get("prepare_error")):
            freeze_frame_item(row, runs_dir, generation_id)


def verify_frames(item: dict, runs_dir: Path = RUNS_DIR) -> None:
    rows = frame_items(item)
    if not rows:
        raise ValueError("没有可评估的录屏数据组")
    for row in rows:
        verify_frame_item(row, runs_dir)


def fork_items(source: dict, indices: list[int], *, reuse_frames: bool, runs_dir: Path = RUNS_DIR) -> list[dict]:
    output = []
    for index in indices:
        item = deepcopy(source["items"][index])
        for field in ("dataset_status", "dataset_revision", "dataset_source_batch_id"):
            item.pop(field, None)
        item.pop("image_input", None)
        for row in frame_items(item):
            row.pop("prepare_error", None)
            if not reuse_frames:
                for field in FRAME_FIELDS:
                    row.pop(field, None)
        if reuse_frames:
            freeze_frames(item, strict=True, runs_dir=runs_dir)
        output.append(item)
    return output


def evaluation_snapshot(cfg: AppConfig) -> dict:
    # 保存真实模板文本，不保存环境变量、API Key 或整份含凭据的 AppConfig。
    syntax = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
    templates = {}
    for node in syntax.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"OPERATION_SYSTEM", "OPERATION_USER", "OPERATION_MULTI_USER"}:
                    templates[target.id] = ast.literal_eval(node.value.args[0])
    if set(templates) != {"OPERATION_SYSTEM", "OPERATION_USER", "OPERATION_MULTI_USER"}:
        raise ValueError("无法保存完整的任务类 Prompt 模板快照")
    value = {
        "operation_skill": cfg.domain_skills["operation"].model_dump(mode="json"),
        "expert_knowledge": cfg.expert_knowledge["operation"].model_dump(mode="json") if cfg.expert_knowledge.get("operation") else None,
        "rubrics": [dim.model_dump(mode="json") for dim in cfg.rubrics],
        "ensemble": cfg.ensemble.model_dump(mode="json"),
        "templates": templates,
    }
    return {**value, "policy_hash": hashlib.sha256(_json_bytes(value)).hexdigest()}


def frozen_evaluation_config(cfg: AppConfig, snapshot: dict) -> AppConfig:
    return cfg.model_copy(update={
        "domain_skills": {**cfg.domain_skills, "operation": DomainSkill.model_validate(snapshot["operation_skill"])},
        "expert_knowledge": {**{key: value for key, value in cfg.expert_knowledge.items() if key != "operation"}, **({"operation": ExpertKnowledgeBase.model_validate(snapshot["expert_knowledge"])} if snapshot.get("expert_knowledge") else {})},
        "rubrics": [RubricDim.model_validate(row) for row in snapshot["rubrics"]],
        "ensemble": EnsembleConfig.model_validate(snapshot["ensemble"]),
    })
