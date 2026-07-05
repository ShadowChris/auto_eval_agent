import pytest

from auto_eval.config import JudgeConfig, RubricDim
from auto_eval.judges.base import JudgeReply
from auto_eval.judges.pairwise_judge import PairwiseJudge
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.schema import EvalItem


class InvalidJsonClient:
    def __init__(self):
        self.cfg = JudgeConfig(name="invalid", runner="openai_compat")
        self.model = "fake-model"
        self.persona = "test"
        self.repair_calls = 0

    async def complete(self, system: str, user: str) -> JudgeReply:
        return JudgeReply(content="这不是合法 JSON")

    async def repair_json(self, malformed_output: str, **kwargs) -> str:
        self.repair_calls += 1
        return "仍然不是合法 JSON"


class RepairableJsonClient(InvalidJsonClient):
    async def repair_json(self, malformed_output: str, **kwargs) -> str:
        self.repair_calls += 1
        return (
            '{"rubric":{"准确性":{"total":5,"reason":"答案正确"}},'
            '"total":5,"correctness":"right","error_type":null,'
            '"rationale":"答案正确"}'
        )


@pytest.mark.asyncio
async def test_rubric_judge_does_not_silently_fallback_to_unclear():
    judge = RubricJudge(
        InvalidJsonClient(),
        [RubricDim(name="准确性", description="是否准确", weight=1, scale=5)],
    )

    with pytest.raises(ValueError, match="无法解析"):
        await judge.score(EvalItem(id="q1", question="1+1=?"), "answer", "2")


@pytest.mark.asyncio
async def test_pairwise_judge_does_not_silently_fallback_to_tie():
    judge = PairwiseJudge(InvalidJsonClient())

    with pytest.raises(ValueError, match="无法解析"):
        await judge.compare_once(
            EvalItem(id="q1", question="哪个更好？"),
            "A",
            "回答A",
            "B",
            "回答B",
        )


@pytest.mark.asyncio
async def test_rubric_judge_repairs_json_without_restarting_agent_loop():
    client = RepairableJsonClient()
    judge = RubricJudge(
        client,
        [RubricDim(name="准确性", description="是否准确", weight=1, scale=5)],
    )

    score = await judge.score(EvalItem(id="q1", question="1+1=?"), "answer", "2")

    assert client.repair_calls == 1
    assert score.correctness == "right"
    assert score.total == 5
