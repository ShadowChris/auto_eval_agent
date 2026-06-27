"""Rubric 盲打分裁判：意图理解 + 理想锚定 + 多角度分析的深度盲评（可联网）。"""
from __future__ import annotations

import time
from datetime import datetime

from ..config import RubricDim
from ..schema import EvalItem, SingleScore
from .base import JudgeClient
from .prompts import (
    RUBRIC_COMPARE_SYSTEM,
    RUBRIC_COMPARE_USER,
    RUBRIC_PROCESS_SYSTEM,
    RUBRIC_PROCESS_USER,
    RUBRIC_SYSTEM,
    RUBRIC_USER,
    parse_analysis,
    parse_json_loose,
)

_VALID = {"right", "wrong", "partial", "unclear"}


def _flatten_rubric(raw, dim_names=None):
    """将嵌套（一级→二级）rubric 展平为一级分 dict，并提取每个一级维度的打分理由。
    兼容旧格式（直接分 / 无 reason）。返回 (rubric, reasons)。"""
    out = {}
    reasons: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict):
            if "total" in v and isinstance(v["total"], (int, float)) and not isinstance(v["total"], bool):
                out[k] = int(v["total"])
            else:
                nums = [x for x in v.values() if isinstance(x, (int, float)) and not isinstance(x, bool)]
                out[k] = round(sum(nums) / len(nums)) if nums else 0
            if v.get("reason"):
                reasons[k] = str(v["reason"])
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
    if dim_names and out and set(out.keys()) != set(dim_names):
        vals = list(out.values())
        if len(vals) == len(dim_names):
            out = {dim_names[i]: vals[i] for i in range(len(dim_names))}
    return out, reasons


class RubricJudge:
    def __init__(self, client: JudgeClient, dims: list[RubricDim], skill_router=None):
        self.client = client
        self.dims = dims
        self.scale = dims[0].scale if dims else 5
        self.skill_router = skill_router

    async def score(self, item: EvalItem, model_name: str, answer: str, run_idx: int = 0,
                    eval_mode: str = "result", process_dims=None, competitor: str | None = None) -> SingleScore:
        today = datetime.now().strftime("%Y年%m月%d日")
        # 自动垂域分类：未标记 category 时让裁判分析 query 类型，并记录来源
        if not item.category or item.category == "default":
            label = await self._classify(item)
            if label:
                item.category = label
                item.metadata["category_source"] = "auto_classified"
            else:
                item.metadata.setdefault("category_source", "fallback_default")
        else:
            item.metadata.setdefault("category_source", "dataset")
        skill_dims, skill_rules, _ = (self.skill_router.match(item) if self.skill_router else (None, "", []))
        is_product_compare = (self.client.cfg.persona == "product_expert") and bool(competitor)
        if eval_mode == "process" and process_dims and item.trace:
            dims = process_dims  # 过程盲评维度不变（不受垂域 skill 影响）
            system = RUBRIC_PROCESS_SYSTEM.render(
                persona=self.client.persona, scale=dims[0].scale if dims else 5, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_PROCESS_USER.render(
                question=item.question, context=item.context, answer=answer, trace=item.trace, current_date=today
            )
        elif is_product_compare:
            dims = skill_dims or self.dims  # 产品专家：待评 vs 竞品 对比盲评
            system = RUBRIC_COMPARE_SYSTEM.render(
                persona=self.client.persona, scale=self.scale, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_COMPARE_USER.render(
                question=item.question, context=item.context, model_name=model_name, answer=answer,
                competitor=competitor, current_date=today
            )
        else:
            dims = skill_dims or self.dims  # 垂域 skill 维度优先
            system = RUBRIC_SYSTEM.render(
                persona=self.client.persona, scale=self.scale, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_USER.render(
                question=item.question, context=item.context, model_name=model_name, answer=answer, current_date=today
            )
        t0 = time.perf_counter()
        reply = await self.client.complete(system, user)
        latency = int((time.perf_counter() - t0) * 1000)

        analysis = parse_analysis(reply.content)
        data = parse_json_loose(reply.content)
        if data is None:
            raise ValueError("裁判输出无法解析为 JSON")
        rubric_raw = data.get("rubric") or {}
        rubric, rubric_reasons = _flatten_rubric(rubric_raw, dim_names=[d.name for d in dims])
        if data.get("total") is not None:
            total = float(data["total"])
        else:
            total = sum(rubric.values()) / len(rubric) if rubric else 0.0
        correctness = data.get("correctness", "unclear")
        if correctness not in _VALID:
            correctness = "unclear"
        error_type = data.get("error_type")
        rationale = data.get("rationale", "")

        return SingleScore(
            item_id=item.id,
            model=model_name,
            judge=self.client.cfg.name,
            persona=self.client.cfg.persona,
            run_idx=run_idx,
            rubric=rubric,
            rubric_reasons=rubric_reasons,
            total=total,
            correctness=correctness,
            error_type=error_type,
            rationale=rationale,
            analysis=analysis,
            used_search=reply.used_search,
            tool_trace=reply.tool_trace,
            search_queries=reply.search_queries,
            truncated=reply.truncated,
            latency_ms=latency,
        )

    def _skill_labels(self) -> list[tuple[str, str]]:
        """候选 (name, 展示文字)：展示文字优先 Skill 的 display，否则回落 name；去掉 default。
        动态来自 config/skills/*.yaml，新增/改名 Skill 后自动跟随，无需手写。"""
        if not self.skill_router:
            return []
        return [
            (s.name, s.display or s.name)
            for s in self.skill_router.domain.values()
            if s.name and s.name != "default"
        ]

    @staticmethod
    def _normalize_label(text: str, labels: list[tuple[str, str]], fallback: str) -> str | None:
        """把 LLM 分类输出归一化为 skill name。
        命中兜底出口(fallback)或无法识别 → None，让 category 保持 default、路由走 default 兜底；
        命中某 skill 的 name 或 display → 返回该 name（SkillRouter 按 name 匹配）。
        """
        if fallback in text:
            return None
        for name, disp in labels:
            if name in text or disp in text:
                return name
        return None

    async def _classify(self, item: EvalItem) -> str | None:
        """轻量垂域分类：未标记 category 时，让裁判从各 Skill 的展示名里选一个；
        若不属于任何一类，输出 default 的展示名（如"通用"）以回落 default。
        候选动态来自 config/skills/*.yaml。只用一次 LLM 调用、不调工具；
        max_tokens=50 防止推理模型被 reasoning 吃光 token。"""
        labels = self._skill_labels()  # [(name, display_text), ...]
        if not labels:
            return None  # 未配置 Skill → 不分类，回落 default
        # default 作为"不属于任何一类"的兜底出口词（取其 display，缺省为"通用"）
        default_skill = self.skill_router.domain.get("default")
        fallback = default_skill.display if default_skill and default_skill.display else "通用"
        shown = " / ".join(d for _, d in labels)
        definitions = []
        for name, display in labels:
            skill = self.skill_router.domain.get(name)
            rule = (skill.rules or "").strip() if skill else ""
            definitions.append(f"- {display}：{rule or '按用户核心意图判断是否属于该类'}")
        system = (
            "你是查询意图分类器。请理解用户真正希望得到的结果，而不是只匹配关键词。\n"
            f"只能从以下标签中选择一个：{shown} / {fallback}。\n\n"
            "类别说明：\n" + "\n".join(definitions) + "\n"
            f"- {fallback}：无法明确归入上述类别的通用问答。\n\n"
            "分类原则：\n"
            "1. 优先按用户问题的核心对象和最终交付物分类，不按单个关键词分类。\n"
            "2. 垂直主题优先：手机/电脑归数码3C，车型归汽车，赛事归体育，歌曲归音乐，影视作品归影视。\n"
            "3. 新闻只用于公共事件、时政、财经和社会热点；某款手机或汽车的发布参数仍归对应垂域。\n"
            "4. 搜索只用于用户明确要求找网页、链接、资料、出处或资源；直接回答某垂域事实仍归对应垂域。\n"
            "5. 文档用于基于给定文件内容的摘要、抽取、比较、改写或问答。\n"
            "6. LBS（旅行规划）用于路线、行程、地点、酒店、景点、餐饮和导航规划。\n"
            "7. 同时包含多个意图时，以用户最终希望你交付的核心结果分类。\n\n"
            "对比例子：\n"
            "- “哪些手机支持卫星通信？” → 数码3C\n"
            "- “帮我找华为手机参数官网链接” → 搜索\n"
            "- “某手机今天发布了什么配置？” → 数码3C\n"
            "- “今天有哪些重要科技行业新闻？” → 新闻\n"
            "- “规划上海三日游路线” → LBS（旅行规划）\n"
            "- “总结这份PDF的结论” → 文档\n"
            "- “这首歌属于什么风格？” → 音乐\n"
            "- “这部电影适合儿童看吗？” → 影视\n\n"
            "在心中完成意图判断后，只输出标签本身，不输出解释、标点或 JSON。"
        )
        try:
            resp = await self.client.client.chat.completions.create(
                model=self.client.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"查询：{item.question}\n\n标签："},
                ],
                temperature=0,
                max_tokens=50,
            )
            text = (resp.choices[0].message.content or "").strip()
            return self._normalize_label(text, labels, fallback)
        except Exception:
            return None
