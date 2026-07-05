"""成对盲比较裁判：匿名 A/B、位置随机化、支持双向。"""
from __future__ import annotations

from datetime import datetime

from ..schema import EvalItem, SinglePair
from .base import JudgeClient, JudgeOutputParseError
from .prompts import PAIRWISE_SYSTEM, PAIRWISE_USER, parse_json_loose, resolve_prompt_context

_SWAP = {"a": "b", "b": "a", "tie": "tie"}


class PairwiseJudge:
    def __init__(self, client: JudgeClient, evaluation_time: datetime | None = None):
        self.client = client
        self.evaluation_time = evaluation_time

    async def compare_once(
        self,
        item: EvalItem,
        model_a: str,
        answer_a: str,
        model_b: str,
        answer_b: str,
        run_idx: int = 0,
        order: str = "ab",
    ) -> SinglePair:
        # order 决定呈现给裁判的左右顺序（抗位置偏差）
        if order == "ab":
            left_text, right_text = answer_a, answer_b
        else:
            left_text, right_text = answer_b, answer_a

        user = PAIRWISE_USER.render(
            question=item.question,
            context=resolve_prompt_context(item.context, self.evaluation_time),
            answer_a=left_text,
            answer_b=right_text,
        )
        reply = await self.client.complete(PAIRWISE_SYSTEM, user)
        data = parse_json_loose(reply.content)
        if data is None:
            repaired = await self.client.repair_json(
                reply.content,
                label="成对裁判输出",
                round_no=reply.rounds + 1,
            )
            data = parse_json_loose(repaired)
            if data is None:
                raise JudgeOutputParseError(
                    "成对裁判输出定向修复后仍无法解析为 JSON",
                    raw_output=reply.content,
                    repair_output=repaired,
                    judge=self.client.cfg.name,
                    model=self.client.model,
                )
        raw = data.get("winner", "tie")
        if raw not in ("a", "b", "tie"):
            raw = "tie"
        # 归一化到固定 (model_a, model_b) 视角：ba 方向需翻转
        winner = raw if order == "ab" else _SWAP[raw]
        return SinglePair(
            item_id=item.id,
            model_a=model_a,
            model_b=model_b,
            judge=self.client.cfg.name,
            run_idx=run_idx,
            order=order,
            winner=winner,
            rationale=data.get("rationale", ""),
        )
