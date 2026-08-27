"""垂域视觉对比裁判：双视频多模态对比两个产品回答的优劣。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import VisualModeProfile
from ..media import encode_frame
from ..schema import VisualCompareObservation
from .base import JudgeClient, JudgeOutputParseError
from .prompts import (
    VISUAL_COMPARE_SYSTEM,
    VISUAL_COMPARE_USER,
    parse_json_loose,
)


def visual_compare_result_fields(
    observation: VisualCompareObservation,
) -> dict[str, Any]:
    """把强类型对比结果转换为 Web/导出友好的扁平字段。"""
    return {
        "relevance": observation.relevance,
        "relevance_reason": observation.relevance_reason,
        "safety": observation.safety,
        "safety_reason": observation.safety_reason,
        "content_quality": observation.content_quality,
        "content_quality_reason": observation.content_quality_reason,
        "need_closure": observation.need_closure,
        "need_closure_reason": observation.need_closure_reason,
        "personalization": observation.personalization,
        "personalization_reason": observation.personalization_reason,
        "has_conflict": observation.has_conflict,
        "conflict_reason": observation.conflict_reason,
        "needs_review": observation.needs_review,
        "needs_review_label": "T" if observation.needs_review else "F",
        "review_reason": observation.review_reason,
        "rationale": observation.rationale,
    }


class VisualCompareJudge:
    """多模态双视频对比裁判：对比两个产品回答录屏的优劣。

    使用与垂域视觉评测相同的多模态模型。
    """

    def __init__(
        self,
        client: JudgeClient,
        profile: VisualModeProfile,
    ):
        self.client = client
        self.profile = profile

    async def evaluate(
        self,
        *,
        question: str,
        context: str = "",
        context1: str = "",
        answer1: str = "",
        frames1: list[str] | None = None,
        context2: str = "",
        answer2: str = "",
        frames2: list[str] | None = None,
        stream_callback=None,
    ) -> dict[str, Any]:
        extraction = self.profile.extraction
        system = VISUAL_COMPARE_SYSTEM.render(
            persona=self.client.persona,
        )
        user = VISUAL_COMPARE_USER.render(
            question=question,
            context=context,
            context1=context1,
            answer1=answer1,
            context2=context2,
            answer2=answer2,
            frame_count1=len(frames1) if frames1 else 0,
            frame_count2=len(frames2) if frames2 else 0,
        )

        # 编码两段帧：产品1在前，产品2在后
        user_images: list[str] = []
        user_image_refs: list[str] = []
        if frames1:
            user_images.extend(
                encode_frame(
                    Path(path),
                    max_edge=extraction.max_edge,
                    quality=extraction.jpeg_quality,
                )
                for path in frames1
            )
            user_image_refs.extend(frames1)
        if frames2:
            user_images.extend(
                encode_frame(
                    Path(path),
                    max_edge=extraction.max_edge,
                    quality=extraction.jpeg_quality,
                )
                for path in frames2
            )
            user_image_refs.extend(frames2)

        started = time.perf_counter()
        raw_output = await self.client.complete(
            system,
            user,
            stream_callback=stream_callback,
            user_images=user_images or None,
            user_image_refs=user_image_refs or None,
        )

        data = parse_json_loose(raw_output)
        repaired = ""
        if data is None:
            repaired = await self.client.repair_json(
                raw_output,
                label="垂域视觉对比评测输出",
                round_no=2,
            )
            data = parse_json_loose(repaired)
        if data is None:
            raise JudgeOutputParseError(
                "垂域视觉对比评测输出无法解析为 JSON",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            )

        # 字段名映射：prompt 中使用 content_conflict，schema 中为 has_conflict
        if "content_conflict" in data and "has_conflict" not in data:
            data["has_conflict"] = data.pop("content_conflict")

        try:
            observation = VisualCompareObservation.model_validate(data)
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"垂域视觉对比评测字段不合法：{exc}",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        result = visual_compare_result_fields(observation)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
        })
        return result
