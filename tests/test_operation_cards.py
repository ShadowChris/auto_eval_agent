"""Exercise card parsing through real single/group judge entrypoints, without LLM calls."""
import json
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval.judges.base import JudgeReply
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.schema import EvalItem
from auto_eval.judges.skill_router import SkillRouter


@pytest.mark.parametrize("multi_group", [False, True])
async def test_operation_judge_passes_card_catalog_to_parser(
    fake_judge_cls, monkeypatch, multi_group
) -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    client = fake_judge_cls(config.judges[1])
    assert not hasattr(client, "expert_knowledge")
    card_types = ["calendar", "memo", "document", "unknown_new_card"]
    data = {
        "task_type": "simple", "correctness": "ok", "total": 5,
        "rubric": {"操作完成度": 5, "步骤正确性": 5},
        "cards": [{"type": key, "suitability": "suitable",
                   "relation_to_query": "direct", "evidence_frames": [1]}
                  for key in card_types],
        "rationale": "测试卡片枚举保留",
    }

    async def complete(system, user, **kwargs):
        assert "document" in system
        assert "小艺帮记保存确认中，蓝色“去查看”是有效保存链接" in system
        assert "图片技能—导航类仅交付对应 App 页面" in system
        assert "不限小艺页面" in system
        assert "黑字判 nok / 任务结果错误" in system
        output = ({"results": [dict(data, group_id=key) for key in ("a", "b")]}
                  if multi_group else data)
        return JudgeReply(content=json.dumps(output, ensure_ascii=False))

    monkeypatch.setattr(client, "complete", complete)
    monkeypatch.setattr("auto_eval.judges.rubric_judge.encode_frame", lambda path: "data:image/jpeg;base64,test")
    judge = RubricJudge(client, config.rubrics, SkillRouter(config.domain_skills),
                        expert_knowledge=config.expert_knowledge["operation"])
    item = EvalItem(id="card-case", question="保存这条记录", category="operation")
    if multi_group:
        scores = (await judge.score_operation_groups(item, [
            {"group_id": key, "item": {"id": key, "frames": ["frame.jpg"]}}
            for key in ("a", "b")
        ])).values()
    else:
        scores = [await judge.score(item, "answer", "已保存", eval_mode="operation")]
    for score in scores:
        assert score.correctness == "ok"
        assert [card.type for card in score.cards] == [
            "calendar", "memo", "document", "other",
        ]
        assert [card.observed_name for card in score.cards] == [
            "日程卡", "备忘录卡", "文档卡", "其他卡",
        ]
