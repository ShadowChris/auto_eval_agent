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

    needs_review = bool(
        observation.needs_review or coverage != "complete"
    )
    return {
        "visual_findings": observation.model_dump(),
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
