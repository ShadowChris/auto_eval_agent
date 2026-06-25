"""Skill 路由：按 EvalItem.category 匹配垂域评测 Skill。

每个 Skill 自带 rubrics（一级+二级维度），匹配后直接返回——不依赖全局维度库。
"""
from __future__ import annotations

from ..config import DomainSkill, RubricDim
from ..schema import EvalItem


class SkillRouter:
    def __init__(self, domain_skills: dict[str, DomainSkill]):
        self.domain = domain_skills  # {skill_name: DomainSkill}

    def resolve(self, item: EvalItem) -> str:
        """返回 item 命中的 skill name；无匹配回落 'default'。

        匹配键：item.category 等于 skill 的 name，或落在其 matching_categories 内。
        前者承接 _classify 返回的 name，后者兼容数据集自带的中文类目。
        """
        cats = item.categories()
        for skill_name, skill in self.domain.items():
            if skill_name == "default":
                continue
            if (skill.name and skill.name in cats) or any(c in skill.matching_categories for c in cats):
                return skill_name
        return "default"

    def display_of(self, skill_name: str) -> str:
        """skill name → 展示名（display 优先，否则 name；未知/default 回落 '通用'）。"""
        s = self.domain.get(skill_name)
        if s and s.display:
            return s.display
        return "通用" if skill_name == "default" else skill_name


    def match(self, item: EvalItem) -> tuple[list[RubricDim], str, list[str]]:
        """返回 (维度列表, 规则文字, 示例列表)。无匹配回落 default。"""
        skill = self.domain.get(self.resolve(item))
        if skill:
            return skill.rubrics, skill.rules or "", list(skill.examples or [])
        return [], "", []
