"""任务类专家经验的渲染、草稿保存与发布。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from .config import ExpertKnowledgeBase


EXPERT_KNOWLEDGE_INTRO = (
    "以下是可信的产品能力、前置条件和界面语义知识，仅使用与当前任务直接相关的条目。"
    "专家经验可以帮助解释录屏，但不能代替任务完成证据；"
    "判断能力范围时，专家经验优先于 Agent 自述；判断本次执行状态时，以录屏中的直接证据为准。"
)


def render_expert_knowledge(knowledge: ExpertKnowledgeBase | None) -> str:
    """渲染普通裁判、主席仲裁和 Web 预览共用的精简文本。"""
    if knowledge is None:
        return ""
    lines = ["【专家经验】", EXPERT_KNOWLEDGE_INTRO, ""]
    for category in knowledge.categories:
        lines.append(f"### {category.name}")
        lines.extend(f"- {rule}" for rule in category.rules)
        lines.append("")
    return "\n".join(lines).rstrip()


def _read(path: Path) -> ExpertKnowledgeBase:
    with path.open("r", encoding="utf-8") as handle:
        return ExpertKnowledgeBase(**(yaml.safe_load(handle) or {}))


def _write_atomic(path: Path, knowledge: ExpertKnowledgeBase) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = knowledge.model_dump(mode="json")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class ExpertKnowledgeStore:
    """已发布配置位于 config，Web 草稿位于 runs，发布时原子替换。"""

    def __init__(self, published_path: Path, draft_path: Path):
        self.published_path = published_path
        self.draft_path = draft_path

    def published(self) -> ExpertKnowledgeBase:
        return _read(self.published_path)

    def draft(self) -> ExpertKnowledgeBase | None:
        return _read(self.draft_path) if self.draft_path.exists() else None

    def save_draft(self, knowledge: ExpertKnowledgeBase) -> ExpertKnowledgeBase:
        current_version = self.published().version
        normalized = knowledge.model_copy(update={"version": current_version})
        _write_atomic(self.draft_path, normalized)
        return normalized

    def publish(self) -> ExpertKnowledgeBase:
        draft = self.draft()
        if draft is None:
            raise FileNotFoundError("没有可发布的专家经验草稿")
        published = draft.model_copy(update={"version": self.published().version + 1})
        _write_atomic(self.published_path, published)
        self.draft_path.unlink(missing_ok=True)
        return published

    def discard_draft(self) -> None:
        self.draft_path.unlink(missing_ok=True)
