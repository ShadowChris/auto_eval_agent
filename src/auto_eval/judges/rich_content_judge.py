"""垂域视觉评测视频识别裁判。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import VisualModeProfile
from ..media import encode_frame
from ..schema import RichContentObservation
from .base import JudgeClient, JudgeOutputParseError
from .prompts import (
    RICH_CONTENT_QUALITY_SYSTEM,
    RICH_CONTENT_QUALITY_USER,
    RICH_CONTENT_SYSTEM,
    RICH_CONTENT_USER,
    parse_analysis,
    parse_json_loose,
)


def rich_content_result_fields(
    observation: RichContentObservation,
    prompt_variant: str = "rich_content",
) -> dict[str, Any]:
    """把强类型视觉发现转换为 Web/导出友好的稳定字段。

    prompt_variant:
      - "rich_content"：垂域视觉评测，Part 2 为整体评价（是否解决用户问题）
      - "rich_content_quality"：垂域视觉综合评测，Part 2 为逐卡适配性评价
    """
    cards = [card.model_dump() for card in observation.cards]
    superlinks = [link.model_dump() for link in observation.superlinks]
    coverage = observation.answer_coverage

    card_presence = "present" if cards else (
        "absent" if coverage == "complete" else "unclear"
    )
    superlink_presence = "present" if superlinks else (
        "absent" if coverage == "complete" else "unclear"
    )
    if coverage == "complete":
        count_type = "exact"
        superlink_count: int | None = len(superlinks)
    elif superlinks:
        count_type = "lower_bound"
        superlink_count = len(superlinks)
    else:
        count_type = "unknown"
        superlink_count = None

    # 中文标签（用于 Excel 导出）
    _presence_label = {"present": "是", "absent": "否", "unclear": "不清楚"}
    card_presence_label = _presence_label.get(card_presence, "")
    superlink_presence_label = _presence_label.get(superlink_presence, "")
    _count_label = {"exact": "精确", "lower_bound": "至少", "unknown": "未知"}
    superlink_count_type_label = _count_label.get(count_type, "")

    # Part 2 字段提取与归一化
    card_suitability = (observation.card_suitability or "").strip()
    if card_suitability not in ("ok", "nok"):
        card_suitability = ""
    card_suitability_reason = observation.card_suitability_reason or ""

    superlink_suitability = (observation.superlink_suitability or "").strip()
    if superlink_suitability not in ("ok", "nok"):
        superlink_suitability = ""
    superlink_suitability_reason = observation.superlink_suitability_reason or ""

    problem_solved_raw = (observation.problem_solved or "").strip()
    _PROBLEM_SOLVED_MAP = {
        "ok": "ok", "是": "ok", "yes": "ok",
        "nok": "nok", "否": "nok", "no": "nok",
        "need_review": "need_review", "不清楚": "need_review", "unclear": "need_review",
    }
    problem_solved = _PROBLEM_SOLVED_MAP.get(
        problem_solved_raw.lower() if problem_solved_raw else "", ""
    )
    problem_solved_reason = observation.problem_solved_reason or ""
    answer_issues = observation.answer_issues or ""

    visual_description = observation.visual_description or ""

    needs_review = bool(
        observation.needs_review or coverage != "complete"
    )

    # 基础字段（两种模式共用）
    base = {
        "visual_findings": observation.model_dump(),
        "visual_description": visual_description,
        "answer_coverage": coverage,
        "card_presence": card_presence,
        "card_presence_label": card_presence_label,
        "card_count": len(cards),
        "card_types": [card["type"] for card in cards],
        "card_contents": [
            card["visible_content"] or card["entity"] for card in cards
        ],
        "superlink_presence": superlink_presence,
        "superlink_presence_label": superlink_presence_label,
        "superlink_count": superlink_count,
        "superlink_count_type": count_type,
        "superlink_count_type_label": superlink_count_type_label,
        "superlink_texts": [link["text"] for link in superlinks],
        "needs_review": needs_review,
        "needs_review_label": "T" if needs_review else "F",
        "review_reason": observation.review_reason,
        "card_suitability": card_suitability,
        "card_suitability_reason": card_suitability_reason,
        "superlink_suitability": superlink_suitability,
        "superlink_suitability_reason": superlink_suitability_reason,
        "rationale": observation.rationale,
    }

    if prompt_variant == "rich_content_quality":
        # 逐卡适配性评价（仅垂域视觉综合评测使用）
        suitability_values = [card["suitability"] for card in cards]
        if not cards:
            card_suitability = "not_applicable"
        elif all(value == "suitable" for value in suitability_values):
            card_suitability = "suitable"
        elif all(value == "unsuitable" for value in suitability_values):
            card_suitability = "unsuitable"
        elif any(value == "unclear" for value in suitability_values):
            card_suitability = "unclear"
        else:
            card_suitability = "partially_suitable"
        scores = [
            int(card["suitability_score"])
            for card in cards
            if card.get("suitability_score") is not None
        ]
        visual_suitability = [
            {
                "type": card["type"],
                "entity": card.get("entity", ""),
                "suitability": card.get("suitability", "unclear"),
                "suitability_score": card.get("suitability_score"),
                "reason": card.get("reason", ""),
            }
            for card in cards
        ]
        base["card_suitability"] = card_suitability
        base["card_suitability_score"] = (
            round(sum(scores) / len(scores), 2) if scores else None
        )
        base["visual_suitability"] = visual_suitability
        # 整体评价字段（quality 模式可能为空）
        base["problem_solved"] = problem_solved
        base["problem_solved_reason"] = problem_solved_reason
        base["answer_issues"] = answer_issues
    else:
        # 垂域视觉评测：Part 2 为整体评价
        base["problem_solved"] = problem_solved
        base["problem_solved_reason"] = problem_solved_reason
        base["answer_issues"] = answer_issues

    return base


def _format_visual_findings_for_rubric(visual: dict) -> str:
    """将 RichContentJudge.evaluate() 返回值中的纯客观视觉描述转为 rubric 裁判可读的自然语言上下文。

    重要：只使用 Part 1（visual_description 纯客观描述），不包含 Part 2（suitability 评价），
    避免视觉评测的结论干扰垂域问答类裁判的独立判断。

    注意：传入的是 evaluate() 的完整返回值（含 visual_findings 嵌套），
    本函数优先从 visual["visual_findings"]["visual_description"] 提取纯描述；
    若为空则回退到从 cards/superlinks 构建纯客观描述（兼容旧模型输出）。"""
    findings = visual.get("visual_findings") or {}

    # 优先使用 Part 1 纯客观描述
    visual_description = findings.get("visual_description", "")
    if not visual_description:
        # 兼容顶层 visual_description
        visual_description = visual.get("visual_description", "")

    if visual_description:
        lines = [
            "【经视觉识别确认的富内容组件】",
            "以下描述来自独立视觉识别，仅客观描述回答中出现的挂卡和Superlink内容。",
            "评分时请将纯文本回答与所有这些富内容组件视为一个整体来评判——",
            "文本可能有意简洁，因为卡片已经承载了详细数据。",
            "不要在\"完整性\"维度上因文本简短而扣分——请检查卡片是否已补充了必要信息。",
            "如果以下显示\"未识别到\"，则此回答确实没有对应富内容组件，按常规纯文本回答评判。",
            "",
            visual_description,
        ]
    else:
        # 兼容旧格式：从 cards/superlinks 构建纯客观描述（不包含 suitability 评价）
        cards = findings.get("cards") or []
        superlinks = findings.get("superlinks") or []

        lines = [
            "【经视觉识别确认的富内容组件】",
            "以下信息来自独立视觉识别，仅客观描述回答中出现的挂卡和Superlink内容。",
            "评分时请将纯文本回答与所有这些富内容组件视为一个整体来评判——",
            "文本可能有意简洁，因为卡片已经承载了详细数据。",
            "不要在\"完整性\"维度上因文本简短而扣分——请检查卡片是否已补充了必要信息。",
            "如果以下显示\"未识别到\"，则此回答确实没有对应富内容组件，按常规纯文本回答评判。",
        ]

        if cards:
            lines.append(f"\n挂卡（共 {len(cards)} 张）：")
            for i, card in enumerate(cards, 1):
                lines.append(f"  {i}. 类型：{card.get('type', '未知')}")
                if card.get("entity"):
                    lines.append(f"     核心实体：{card['entity']}")
                if card.get("visible_content"):
                    lines.append(f"     可见内容：{card['visible_content']}")
                # 注意：不输出 relation_to_query / suitability 等评价字段
        else:
            lines.append("\n未识别到挂卡。")

        if superlinks:
            lines.append(f"\n蓝色Superlink（共 {len(superlinks)} 个）：")
            for i, link in enumerate(superlinks, 1):
                lines.append(f"  {i}. 文字：{link.get('text', '')}")
        else:
            lines.append("\n未识别到蓝色Superlink。")

    coverage = findings.get("answer_coverage", visual.get("answer_coverage", "unclear"))
    coverage_note = {
        "complete": "（关键帧覆盖完整回答区域，以上识别结果可靠）",
        "partial": "（关键帧只覆盖部分回答区域，以上为已识别部分，可能还有未识别内容）",
        "unclear": "（关键帧覆盖不足，识别结果仅供参考）",
    }.get(coverage, "")
    lines.append(f"\n回答覆盖度：{coverage}{coverage_note}")

    return "\n".join(lines)


class RichContentJudge:
    """单视觉裁判：识别挂卡和 Superlink，并返回结构化发现。

    prompt_variant 可选值：
    - "rich_content"（默认）：使用 RICH_CONTENT_SYSTEM / RICH_CONTENT_USER
    - "rich_content_quality"：使用独立的 RICH_CONTENT_QUALITY_SYSTEM / RICH_CONTENT_QUALITY_USER
    """

    def __init__(
        self,
        client: JudgeClient,
        profile: VisualModeProfile,
        prompt_variant: str = "rich_content",
    ):
        self.client = client
        self.profile = profile
        self._prompt_variant = prompt_variant
        if prompt_variant == "rich_content_quality":
            self._system_template = RICH_CONTENT_QUALITY_SYSTEM
            self._user_template = RICH_CONTENT_QUALITY_USER
        else:
            self._system_template = RICH_CONTENT_SYSTEM
            self._user_template = RICH_CONTENT_USER

    async def evaluate(
        self,
        *,
        question: str,
        context: str,
        answer_text: str,
        frames: list[str],
        stream_callback=None,
    ) -> dict[str, Any]:
        extraction = self.profile.extraction
        system = self._system_template.render(
            persona=self.client.persona,
            card_types=self.profile.card_types,
            suitability_anchors=self.profile.suitability_anchors,
        )
        user = self._user_template.render(
            question=question,
            context=context,
            answer_text=answer_text,
            frame_count=len(frames),
        )
        user_images = [
            encode_frame(
                Path(path),
                max_edge=extraction.max_edge,
                quality=extraction.jpeg_quality,
            )
            for path in frames
        ]
        started = time.perf_counter()
        reply = await self.client.complete(
            system,
            user,
            stream_callback=stream_callback,
            user_images=user_images or None,
            user_image_refs=frames or None,
        )
        data = parse_json_loose(reply.content)
        repaired = ""
        if data is None:
            repaired = await self.client.repair_json(
                reply.content,
                label="垂域视觉评测识别输出",
                round_no=reply.rounds + 1,
            )
            data = parse_json_loose(repaired)
        if data is None:
            raise JudgeOutputParseError(
                "垂域视觉评测识别输出无法解析为 JSON",
                raw_output=reply.content,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            )
        try:
            observation = RichContentObservation.model_validate(data)
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"垂域视觉评测识别字段不合法：{exc}",
                raw_output=reply.content,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        result = rich_content_result_fields(observation, prompt_variant=self._prompt_variant)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "used_search": reply.used_search,
            "tool_trace": reply.tool_trace,
            "truncated": reply.truncated,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
            "analysis": parse_analysis(reply.content),
        })
        return result
