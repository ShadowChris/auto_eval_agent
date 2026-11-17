"""完成态静止录屏属于输入复核，不伪造成任务完成或执行失败。"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_eval.analysis.operation_statistics import summarize_operation_results
from auto_eval.config import load_config
from auto_eval.judges.arbitrator import Arbitrator
from auto_eval.judges.base import JudgeReply
from auto_eval.judges.ensemble import aggregate_operation_scores
from auto_eval.judges.operation_fields import (
    COMPLETED_REPLY_RECORDING_CATEGORY as CATEGORY,
    LEGACY_REVIEW_CATEGORIES,
    QUERY_ALIGNMENT_ISSUE,
    normalize_operation_fields,
)
from auto_eval.judges.prompts import ARBITRATOR_SYSTEM, OPERATION_SYSTEM, RUBRIC_SYSTEM
from auto_eval.judges.rubric_judge import RubricJudge, _operation_score_from_data
from auto_eval.schema import EvalItem
from auto_eval.web.history import export_rows, jsonl_export_rows
from auto_eval.web.operation_comparison_import import import_operation_comparison_file
from auto_eval.web.runner import _fill_operation_verdict


@pytest.fixture(scope="module")
def config():
    return load_config(Path(__file__).resolve().parents[1] / "config")


@pytest.mark.parametrize("multi_group,multi_turn", [(False, False), (True, False), (False, True)])
def test_policy_is_shared_by_modes_with_narrow_visual_boundaries(config, multi_group, multi_turn):
    skill = config.domain_skills["operation"]
    policy = skill.operation_policy
    assert policy.issue_types[CATEGORY].allowed_correctness == ["others"]
    assert CATEGORY in policy.review_category_names and CATEGORY in LEGACY_REVIEW_CATEGORIES
    prompt = OPERATION_SYSTEM.render(persona="终端用户", dims=skill.rubrics, scale=5,
        policy=policy, expert_knowledge_text="", multi_group=multi_group,
        multi_turn=multi_turn, latest_turn=multi_turn)
    first_gate = ("只对当前轮执行 Query 一致性门禁"
                  if multi_turn and getattr(policy, "multi_turn_rules", []) else policy.decision_order[0])
    assert prompt.index(first_gate) < prompt.index(policy.decision_order[1])
    assert CATEGORY in policy.decision_order[1]
    for boundary in ("当前轮有效录屏从首帧", "后续始终停留同一主对话完成态", "时钟、光标",
                     "已展示本轮发送、生成、操作或业务状态变化后停留结果页，不命中",
                     "正常静态系统页面", "幂等设置初始状态已满足",
                     "多轮只检查当前轮对应帧", "只有一张结果图", "所有维度和总分填 null"):
        assert boundary in prompt
    arbitration = ARBITRATOR_SYSTEM.render(operation_mode=True, dims=skill.rubrics,
        policy=policy, expert_knowledge_text="")
    assert policy.issue_types[CATEGORY].description in arbitration
    assert CATEGORY not in RUBRIC_SYSTEM.render(persona="终端用户", dims=skill.rubrics, scale=5)


@pytest.mark.parametrize("raw", ["ok", "nok", "no_support", "others"])
@pytest.mark.parametrize("field", ["issue_types", "need_review_cate"])
def test_input_gate_keeps_only_review_and_removes_all_scores(config, raw, field):
    skill = config.domain_skills["operation"]
    data = dict(correctness=raw, task_type="simple", is_low_level="yes", total=5,
                rubric={dim.name: {"total": 5, "reason": "错误评分"} for dim in skill.rubrics},
                issue_types=["任务结果错误"], need_review_cate=[], rationale="首尾均是同一已完成回答")
    data[field].append(CATEGORY)
    score = _score(config, data)
    assert score.correctness == "others" and score.is_low_level == "no"
    assert score.need_review_cate == [CATEGORY] and score.issue_types == []
    assert score.total is None and score.rubric == {} and score.rubric_reasons == {}
    assert set(score.na_dimensions) == {dim.name for dim in skill.rubrics}
    # A dissenting scored judge must not leak dimension scores into a gate verdict.
    good = _score(config, dict(correctness="ok", rubric={dim.name: 5 for dim in skill.rubrics}))
    verdict = aggregate_operation_scores([score, deepcopy(score), good], skill.rubrics,
        config.ensemble, 0.6, skill.operation_policy.issue_types)
    assert verdict.total is None and verdict.rubric == {}
    result = {}
    _fill_operation_verdict(result, verdict)
    assert result["correctness"] == "no_support" and result["judge_correctness"] == "others"
    assert result["need_review_cate"] == [CATEGORY] and result["issue_types"] == []


def _score(config, data):
    skill = config.domain_skills["operation"]
    return _operation_score_from_data(data, item_id="static-reply", model_name="fake-model",
        judge=SimpleNamespace(cfg=SimpleNamespace(name="end_user", persona="终端用户")),
        dims=skill.rubrics, policy=skill.operation_policy, reply=JudgeReply(content=""), latency=10)


def test_query_alignment_is_still_first_and_no_text_only_detection(config):
    policy = config.domain_skills["operation"].operation_policy
    assert normalize_operation_fields("ok", [CATEGORY, QUERY_ALIGNMENT_ISSUE], "yes", "simple",
        policy.issue_types) == ("others", [QUERY_ALIGNMENT_ISSUE], "no")
    # Normalization never infers a frozen recording from a terse answer or absent steps.
    for raw, issues in (("ok", []), ("nok", ["执行中断"]), ("others", ["未展示可验证结果"])):
        status, normalized, _ = normalize_operation_fields(raw, issues, "no", "simple", policy.issue_types)
        assert status == raw and CATEGORY not in normalized


@pytest.mark.asyncio
async def test_multi_group_gate_does_not_change_other_groups(config, monkeypatch):
    class Client:
        cfg = SimpleNamespace(name="end_user", persona="end_user")
        persona = "终端用户"
        model = "fake-model"

        async def complete(self, system, user, **kwargs):
            assert CATEGORY in system and len(kwargs["user_images"]) == 4
            return JudgeReply(content=json.dumps({"results": [
                dict(group_id="a", correctness="ok", total=5, rubric={"操作完成度": 5},
                     need_review_cate=[CATEGORY], rationale="首尾保持同一完成态"),
                dict(group_id="b", correctness="ok", total=5, rubric={"操作完成度": 5},
                     issue_types=[], need_review_cate=[], rationale="已发送并实际打开设置"),
            ]}))

    skill = config.domain_skills["operation"]
    judge = RubricJudge(Client(), config.rubrics, SimpleNamespace(domain={"operation": skill}))
    monkeypatch.setattr("auto_eval.judges.rubric_judge.encode_frame", lambda path: str(path))
    scores = await judge.score_operation_groups(EvalItem(id="multi", question="打开设置"), [
        dict(group_id="a", group_name="A", item=dict(id="a", frames=["a1.jpg", "a2.jpg"])),
        dict(group_id="b", group_name="B", item=dict(id="b", frames=["b1.jpg", "b2.jpg"])),
    ])
    assert scores["a"].correctness == "others" and scores["a"].need_review_cate == [CATEGORY]
    assert scores["a"].rubric == {} and scores["a"].total is None
    assert scores["b"].correctness == "ok" and scores["b"].total == 5
    assert scores["b"].need_review_cate == []


@pytest.mark.asyncio
async def test_arbitration_cannot_reintroduce_scores(config):
    class Client:
        async def complete(self, system, user):
            assert CATEGORY in system
            return JudgeReply(content=json.dumps(dict(correctness="ok", rubric={"操作完成度": 5},
                total=5, need_review_cate=[CATEGORY], issue_types=[], is_low_level="yes")))

    skill = config.domain_skills["operation"]
    result = await Arbitrator(Client()).arbitrate(EvalItem(id="a", question="打开蓝牙"), "已打开", [],
        eval_mode="operation", dims=skill.rubrics, policy=skill.operation_policy)
    assert result["correctness"] == "others" and result["need_review_cate"] == [CATEGORY]
    assert result["rubric"] == {} and result["total"] is None and result["is_low_level"] == "no"


def test_history_exports_imports_and_statistics_preserve_review_reason(config):
    score = _score(config, dict(correctness="others", need_review_cate=[CATEGORY]))
    skill = config.domain_skills["operation"]
    verdict = aggregate_operation_scores([score], skill.rubrics, config.ensemble, 0.6,
        skill.operation_policy.issue_types)
    result = dict(index=0, item_id="a", query="打开蓝牙")
    _fill_operation_verdict(result, verdict)
    snapshot = dict(task_id="static", mode="operation", items=[dict(id="a", query="打开蓝牙")],
                    results=[result])
    before = deepcopy(snapshot)
    assert export_rows(snapshot)["评估结果"][0]["need_review_cate"] == CATEGORY
    row = jsonl_export_rows(snapshot)[0]
    assert row["evaluation"]["need_review_cate"] == [CATEGORY]
    assert snapshot == before
    batch = import_operation_comparison_file("result.jsonl", json.dumps(row).encode())
    assert batch["rows"][0]["result"]["need_review_cate"] == [CATEGORY]
    stats = summarize_operation_results([result, dict(correctness="ok")])
    assert stats["ok_rate_denominator"] == 1 and stats["ok_rate"] == 1.0
    assert stats["review_category_rows"][0]["category"] == CATEGORY
