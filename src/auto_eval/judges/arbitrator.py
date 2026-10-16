"""主席仲裁：裁判分歧时，由主席看全裁判理由做最终裁决（可联网核查）。

触发条件：多裁判一致率/稳定性低于阈值（low_agreement）。
主席综合各方理由 + 自主联网核查 → 给最终判定 + 置信度；不确定给 unclear。
"""
from __future__ import annotations

from datetime import datetime

from ..schema import EvalItem, OperationSingleScore, SingleScore
from ..expert_knowledge import render_expert_knowledge
from .base import JudgeClient, JudgeOutputParseError
from .operation_fields import (
    QUERY_ALIGNMENT_ISSUE,
    collect_operation_output_issues,
    hoist_misnested_operation_fields,
    normalize_operation_cards,
    normalize_operation_fields,
    split_operation_issue_fields,
)
from .prompts import (
    ARBITRATOR_SYSTEM,
    ARBITRATOR_USER,
    parse_analysis,
    parse_json_loose,
    resolve_prompt_context,
)

_VALID = {"right", "wrong", "partial", "unclear"}


class Arbitrator:
    def __init__(self, client: JudgeClient, evaluation_time: datetime | None = None,
                 expert_knowledge=None):
        self.client = client
        self.evaluation_time = evaluation_time
        self.expert_knowledge = expert_knowledge

    async def arbitrate(
        self,
        item: EvalItem,
        answer: str,
        single_scores: list[SingleScore] | list[OperationSingleScore],
        *,
        eval_mode: str | None = None,
        dims=None,
        policy=None,
    ) -> dict:
        operation_mode = eval_mode == "operation"
        system = ARBITRATOR_SYSTEM.render(
            operation_mode=operation_mode,
            dims=dims or [],
            policy=policy,
            expert_knowledge_text=render_expert_knowledge(self.expert_knowledge),
        )
        judges_summary = [
            {
                "name": s.judge,
                "correctness": s.correctness,
                "total": round(s.total, 2) if s.total is not None else None,
                "rubric": s.rubric,
                "error_type": getattr(s, "error_type", None),
                "issue_types": getattr(s, "issue_types", []),
                "need_review_cate": getattr(s, "need_review_cate", []),
                "cards": [card.model_dump(mode="json") for card in getattr(s, "cards", [])],
                "is_low_level": getattr(s, "is_low_level", "no"),
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
        if operation_mode:
            data = hoist_misnested_operation_fields(data)
        rubric = {
            k: int(v) for k, v in (data.get("rubric") or {}).items() if isinstance(v, (int, float))
        }
        total = data.get("total")
        if total is None:
            total = sum(rubric.values()) / len(rubric) if rubric else None
        try:
            confidence = float(data["confidence"]) if data.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        if operation_mode:
            task_type = str(data.get("task_type") or "").strip().lower()
            if task_type not in {"simple", "complex"}:
                task_type = None
            raw_correctness = str(data.get("correctness") or "").strip().lower()
            if raw_correctness not in {"ok", "nok", "no_support", "others", "right", "wrong", "partial", "unclear"}:
                raise ValueError(f"任务类仲裁返回非法 correctness：{raw_correctness!r}")
            correctness, all_issues, is_low_level = normalize_operation_fields(
                data.get("correctness"),
                collect_operation_output_issues(data),
                data.get("is_low_level", "no"),
                task_type,
                policy.issue_types if policy else None,
            )
            issue_types, need_review_cate = split_operation_issue_fields(
                all_issues, policy.review_category_names if policy else []
            )
            if QUERY_ALIGNMENT_ISSUE in need_review_cate:
                rubric = {}
                total = None
            return {
                "task_type": task_type,
                "correctness": correctness,
                "rubric": {k: round(float(v), 2) for k, v in rubric.items()},
                "total": round(float(total), 2) if total is not None else None,
                "issue_types": issue_types,
                "need_review_cate": need_review_cate,
                "cards": normalize_operation_cards(
                    data.get("cards"),
                    {card.key: card.name for card in getattr(self.expert_knowledge, "result_cards", [])},
                ),
                "is_low_level": is_low_level,
                "confidence": confidence,
                "rationale": data.get("rationale", ""),
                "used_search": reply.used_search,
                "tool_trace": reply.tool_trace,
                "analysis": parse_analysis(reply.content),
            }

        correctness = data.get("correctness", "unclear")
        if correctness not in _VALID:
            correctness = "unclear"
        return {
            "correctness": correctness,
            "rubric": {k: round(float(v), 2) for k, v in rubric.items()},
            "total": round(float(total), 2) if total is not None else 0.0,
            "error_type": data.get("error_type"),
            "is_low_level": "no",
            "confidence": confidence,
            "rationale": data.get("rationale", ""),
            "used_search": reply.used_search,
            "tool_trace": reply.tool_trace,
            "analysis": parse_analysis(reply.content),
        }
