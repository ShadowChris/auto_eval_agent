"""主席仲裁：裁判分歧时，由主席看全裁判理由做最终裁决（可联网核查）。

触发条件：多裁判一致率/稳定性低于阈值（low_agreement）。
主席综合各方理由 + 自主联网核查 → 给最终判定 + 置信度；不确定给 unclear。
"""
from __future__ import annotations

from datetime import datetime

from ..schema import EvalItem, SingleScore
from .base import JudgeClient, JudgeOutputParseError
from .operation_fields import normalize_operation_fields
from .prompts import (
    ARBITRATOR_SYSTEM,
    ARBITRATOR_USER,
    parse_analysis,
    parse_json_loose,
    resolve_prompt_context,
)

_VALID = {"right", "wrong", "partial", "unclear"}


class Arbitrator:
    def __init__(self, client: JudgeClient, evaluation_time: datetime | None = None):
        self.client = client
        self.evaluation_time = evaluation_time

    async def arbitrate(
        self,
        item: EvalItem,
        answer: str,
        single_scores: list[SingleScore],
        *,
        eval_mode: str | None = None,
        dims=None,
    ) -> dict:
        operation_mode = eval_mode == "operation"
        system = ARBITRATOR_SYSTEM.render(
            operation_mode=operation_mode,
            dims=dims or [],
        )
        judges_summary = [
            {
                "name": s.judge,
                "correctness": s.correctness,
                "total": round(s.total, 2),
                "rubric": s.rubric,
                "error_type": s.error_type,
                "is_low_level": s.is_low_level,
                "rationale": s.rationale,
                "tool_trace": s.tool_trace,
            }
            for s in single_scores
        ]
        user = ARBITRATOR_USER.render(
            question=item.question,
            context=resolve_prompt_context(item.context, self.evaluation_time),
            answer=answer,
            judges=judges_summary,
            operation_mode=operation_mode,
        )
        reply = await self.client.complete(system, user)
        data = parse_json_loose(reply.content)
        if data is None:
            repaired = await self.client.repair_json(
                reply.content,
                label="仲裁输出",
                round_no=reply.rounds + 1,
            )
            data = parse_json_loose(repaired)
            if data is None:
                raise JudgeOutputParseError(
                    "仲裁输出定向修复后仍无法解析为 JSON",
                    raw_output=reply.content,
                    repair_output=repaired,
                    judge=self.client.cfg.name,
                    model=self.client.model,
                )
        correctness = data.get("correctness", "unclear")
        if correctness not in _VALID:
            correctness = "unclear"
        rubric = {
            k: int(v) for k, v in (data.get("rubric") or {}).items() if isinstance(v, (int, float))
        }
        total = data.get("total")
        if total is None:
            total = sum(rubric.values()) / len(rubric) if rubric else 0.0
        try:
            confidence = float(data["confidence"]) if data.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        error_type = data.get("error_type")
        is_low_level = data.get("is_low_level", "no")
        if operation_mode:
            error_type, is_low_level = normalize_operation_fields(
                correctness,
                error_type,
                is_low_level,
                data.get("task_type"),
            )
        return {
            "correctness": correctness,
            "rubric": {k: round(float(v), 2) for k, v in rubric.items()},
            "total": round(float(total), 2),
            "error_type": error_type,
            "is_low_level": is_low_level,
            "confidence": confidence,
            "rationale": data.get("rationale", ""),
            "used_search": reply.used_search,
            "tool_trace": reply.tool_trace,
            "analysis": parse_analysis(reply.content),
        }
