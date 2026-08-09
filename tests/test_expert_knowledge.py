from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_eval.config import ExpertKnowledgeBase, load_config
from auto_eval.expert_knowledge import ExpertKnowledgeStore, render_expert_knowledge
from auto_eval.web import server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _knowledge(version: int = 1) -> ExpertKnowledgeBase:
    return ExpertKnowledgeBase(
        name="任务类专家经验",
        description="只保存产品事实",
        version=version,
        categories=[{
            "key": "capabilities",
            "name": "已确认支持的能力",
            "description": "能力范围",
            "rules": ["支持打开来电播报。"],
        }],
    )


def test_operation_expert_knowledge_loads_separately_from_policy() -> None:
    config = load_config(PROJECT_ROOT / "config")
    knowledge = config.expert_knowledge["operation"]

    assert knowledge.name == "任务类专家经验"
    assert knowledge.version == 2
    assert any(
        "来电播报功能" in rule
        for category in knowledge.categories
        for rule in category.rules
    )
    assert any(
        "证书与凭据" in rule
        for category in knowledge.categories
        for rule in category.rules
    )
    assert not config.domain_skills["operation"].operation_policy.prior_knowledge


def test_expert_knowledge_rejects_duplicate_categories_and_empty_rules() -> None:
    with pytest.raises(ValidationError, match="key 不能重复"):
        ExpertKnowledgeBase(
            name="重复",
            categories=[
                {"key": "same", "name": "一", "rules": ["规则一"]},
                {"key": "same", "name": "二", "rules": ["规则二"]},
            ],
        )

    with pytest.raises(ValidationError, match="至少需要一条规则"):
        ExpertKnowledgeBase(
            name="空规则",
            categories=[{"key": "empty", "name": "空", "rules": [" "]}],
        )


def test_rendered_knowledge_contains_facts_but_not_management_fields() -> None:
    rendered = render_expert_knowledge(_knowledge(version=7))

    assert "【专家经验】" in rendered
    assert "判断能力范围时，专家经验优先于 Agent 自述" in rendered
    assert "### 已确认支持的能力" in rendered
    assert "支持打开来电播报" in rendered
    assert "capabilities" not in rendered
    assert "version" not in rendered
    assert "只保存产品事实" not in rendered


def test_expert_knowledge_store_separates_draft_and_published_versions(tmp_path: Path) -> None:
    published_path = tmp_path / "config" / "operation.yaml"
    draft_path = tmp_path / "runs" / "operation.yaml"
    store = ExpertKnowledgeStore(published_path, draft_path)
    from auto_eval.expert_knowledge import _write_atomic

    _write_atomic(published_path, _knowledge(version=3))
    draft = _knowledge(version=99)
    draft.categories[0].rules.append("支持开启关怀模式。")

    saved = store.save_draft(draft)
    assert saved.version == 3
    assert store.published().version == 3
    assert store.draft().categories[0].rules[-1] == "支持开启关怀模式。"

    published = store.publish()
    assert published.version == 4
    assert store.draft() is None
    assert store.published().categories[0].rules[-1] == "支持开启关怀模式。"


def test_expert_knowledge_web_api_uses_draft_then_reloads_on_publish(tmp_path, monkeypatch) -> None:
    published_path = tmp_path / "config" / "operation.yaml"
    draft_path = tmp_path / "runs" / "operation.yaml"
    store = ExpertKnowledgeStore(published_path, draft_path)
    from auto_eval.expert_knowledge import _write_atomic

    _write_atomic(published_path, _knowledge())
    monkeypatch.setattr(server, "_operation_knowledge_store", lambda: store)
    reloaded = object()
    monkeypatch.setattr(server, "load_config", lambda _: reloaded)

    payload = server.api_operation_knowledge()
    assert payload["has_unpublished_changes"] is False

    updated = _knowledge()
    updated.categories[0].rules.append("支持开启家人共享。")
    server.api_save_operation_knowledge_draft(updated)
    assert server.api_operation_knowledge()["has_unpublished_changes"] is True

    result = server.api_publish_operation_knowledge()
    assert result["published"]["version"] == 2
    assert server._state["cfg"] is reloaded


def test_expert_knowledge_web_ui_exposes_editor_and_prompt_preview() -> None:
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (server.STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "任务类专家经验" in html
    assert "保存草稿" in html
    assert "发布更改" in html
    assert "Prompt 注入预览" in html
    assert 'fetch("/api/knowledge/operation")' in js
    assert 'fetch("/api/knowledge/operation/draft"' in js
    assert 'fetch("/api/knowledge/operation/publish"' in js
    assert ".knowledge-shell" in css
