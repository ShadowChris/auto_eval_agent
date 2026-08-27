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
    RICH_CONTENT_SYSTEM,
    RICH_CONTENT_USER,
    parse_analysis,
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
    # complete 时数量精确；partial 时只是下界；unclear 且未识别到时数量未知
    superlink_count: int | None = (
        len(superlinks) if (coverage == "complete" or superlinks) else None
    )

    # 中文标签（用于 Excel 导出）
    _presence_label = {"present": "是", "absent": "否", "unclear": "不清楚"}
    card_presence_label = _presence_label.get(card_presence, "")
    superlink_presence_label = _presence_label.get(superlink_presence, "")

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

    needs_review = bool(
        observation.needs_review or coverage != "complete"
    )

    # 导出字段
    base = {
        "turn_summary": observation.turn_summary or "",
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
        "superlink_texts": [link["text"] for link in superlinks],
        "needs_review": needs_review,
        "needs_review_label": "T" if needs_review else "F",
        "review_reason": observation.review_reason,
        "card_suitability": card_suitability,
        "card_suitability_reason": card_suitability_reason,
        "superlink_suitability": superlink_suitability,
        "superlink_suitability_reason": superlink_suitability_reason,
        "problem_solved": problem_solved,
        "problem_solved_reason": problem_solved_reason,
        "answer_issues": answer_issues,
        "rationale": observation.rationale,
    }

    return base


class RichContentJudge:
    """单视觉裁判：识别挂卡和 Superlink，并返回结构化发现。"""

    def __init__(self, client: JudgeClient, profile: VisualModeProfile):
        self.client = client
        self.profile = profile
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
        raw_output = await self.client.complete(
            system,
            user,
            stream_callback=stream_callback,
            user_images=user_images or None,
            user_image_refs=frames or None,
        )
        data = parse_json_loose(raw_output)
        repaired = ""
        if data is None:
            repaired = await self.client.repair_json(
                raw_output,
                label="垂域视觉评测识别输出",
                round_no=2,
            )
            data = parse_json_loose(repaired)
        if data is None:
            raise JudgeOutputParseError(
                "垂域视觉评测识别输出无法解析为 JSON",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            )
        try:
            observation = RichContentObservation.model_validate(data)
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"垂域视觉评测识别字段不合法：{exc}",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        result = rich_content_result_fields(observation)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
            "analysis": parse_analysis(raw_output),
        })
        return result
