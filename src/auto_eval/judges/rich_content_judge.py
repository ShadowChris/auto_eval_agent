"""垂域挂卡 / Superlink 视频视觉识别裁判。"""
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
    RICH_CONTENT_SYSTEM,
    RICH_CONTENT_USER,
    parse_json_loose,
)


def rich_content_result_fields(
    observation: RichContentObservation,
) -> dict[str, Any]:
    """把强类型视觉发现转换为 Web/导出友好的稳定字段。"""
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

    visual_description = observation.visual_description or ""
    # Part 2：视觉评测适配性 — 从 cards 提取，独立于单回答盲评的卡片适配性评价
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

    needs_review = bool(
        observation.needs_review or coverage != "complete"
    )
    return {
        "visual_findings": observation.model_dump(),
        "visual_description": visual_description,
        "visual_suitability": visual_suitability,
        "answer_coverage": coverage,
        "card_presence": card_presence,
        "card_count": len(cards),
        "card_types": [card["type"] for card in cards],
        "card_contents": [
            card["visible_content"] or card["entity"] for card in cards
        ],
        "card_suitability": card_suitability,
        "card_suitability_score": (
            round(sum(scores) / len(scores), 2) if scores else None
        ),
        "superlink_presence": superlink_presence,
        "superlink_count": superlink_count,
        "superlink_count_type": count_type,
        "superlink_texts": [link["text"] for link in superlinks],
        "needs_review": needs_review,
        "review_reason": observation.review_reason,
        "rationale": observation.rationale,
    }


def _format_visual_findings_for_rubric(visual: dict) -> str:
    """将 RichContentJudge.evaluate() 返回值中的纯客观视觉描述转为 rubric 裁判可读的自然语言上下文。

    重要：只使用 Part 1（visual_description 纯客观描述），不包含 Part 2（suitability 评价），
    避免视觉评测的结论干扰单回答盲评裁判的独立判断。

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
    """单视觉裁判：识别挂卡和 Superlink，并返回结构化发现。"""

    def __init__(self, client: JudgeClient, profile: VisualModeProfile):
        self.client = client
        self.profile = profile

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
        system = RICH_CONTENT_SYSTEM.render(
            persona=self.client.persona,
            card_types=self.profile.card_types,
            suitability_anchors=self.profile.suitability_anchors,
        )
        user = RICH_CONTENT_USER.render(
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
                label="挂卡与Superlink视觉识别输出",
                round_no=reply.rounds + 1,
            )
            data = parse_json_loose(repaired)
        if data is None:
            raise JudgeOutputParseError(
                "挂卡与Superlink视觉识别输出无法解析为 JSON",
                raw_output=reply.content,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            )
        try:
            observation = RichContentObservation.model_validate(data)
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"挂卡与Superlink视觉识别字段不合法：{exc}",
                raw_output=reply.content,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        result = rich_content_result_fields(observation)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "used_search": reply.used_search,
            "tool_trace": reply.tool_trace,
            "truncated": reply.truncated,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
        })
        return result
