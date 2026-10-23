"""Policy delivery and result handling; these tests do not simulate visual judging."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_eval.config import load_config
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.ensemble import aggregate_operation_scores
from auto_eval.judges.prompts import OPERATION_SYSTEM, RUBRIC_SYSTEM
from auto_eval.judges.rubric_judge import _operation_score_from_data
from auto_eval.web.runner import _fill_operation_verdict


@pytest.fixture(scope="module")
def config():
    return load_config(Path(__file__).resolve().parents[1] / "config")


@pytest.mark.parametrize("multi_group", [False, True])
@pytest.mark.parametrize("has_query_images", [False, True])
def test_capability_and_evidence_policy_reaches_every_task_prompt(
    config, multi_group, has_query_images,
):
    skill = config.domain_skills["operation"]
    prompt = OPERATION_SYSTEM.render(
        persona="终端用户", dims=skill.rubrics, scale=5,
        policy=skill.operation_policy,
        expert_knowledge_text=render_expert_knowledge(config.expert_knowledge["operation"]),
        multi_group=multi_group, has_query_images=has_query_images,
    )
    assert "所有 Query 默认具备对应执行能力，包括第三方应用业务" in prompt
    assert "不再要求专家经验或同一目标的成功记录反驳该声明" in prompt
    assert "不能因没有操作画面降为 others" in prompt
    assert "一律判 others / 未展示可验证结果，不要求先证明曾尝试" in prompt
    assert "可见最终结果也是直接证据" in prompt
    assert "固定异常话术优先于一般拒绝规则" in prompt
    assert "普通隐私设置和权限开关操作不适用" in prompt
    assert "未作必要澄清却擅自作答" in prompt
    for obsolete in (
        "默认支持范围不得扩大为所有第三方应用能力",
        "且没有可信专家经验或可见证据反驳",
        "agent 明确声明能力不支持且没有可信反证",
        "稳定的能力不支持应使用缺少前置条件",
        "无证据完成声明",
    ):
        assert obsolete not in prompt
    assert ("仅解析或描述原图不属于合格交付" in prompt) == has_query_images
    if has_query_images:
        assert "图片解析可以是检索的中间步骤" in prompt
    # Image rules are conditional; ordinary tasks retain the existing size guard.
    assert len(prompt) < 22500 + (600 if has_query_images else 0)
    assert "所有 Query 默认具备" not in RUBRIC_SYSTEM.render(persona="问答裁判", dims=[], scale=5)


def test_fixed_messages_and_product_endpoints_are_published_together(config):
    knowledge = config.expert_knowledge["operation"]
    categories = {category.key: category for category in knowledge.categories}
    messages = "\n".join(categories["fixed_failure_messages"].rules)
    assert "无法为你继续操作了" in messages
    assert "小艺算力不够了，请稍后再试" in messages
    assert "遇到一点小问题，请稍后再试" in messages
    facts = "\n".join(categories["special_result_semantics"].rules)
    assert "蓝色“去查看”是有效保存链接，黑字缺链接" in facts
    assert "应用下载/卸载以目标应用的可操作页面交付" in facts
    assert "恢复应用仅交付应用管理页面" in facts
    assert "图片技能—导航类仅交付对应 App 页面" in facts


@pytest.mark.parametrize("raw,issues,review,expected", [
    pytest.param("nok", ["应执行目标未执行"], [], "nok", id="unsupported-refusal"),
    pytest.param("nok", ["应执行目标未执行"], ["未展示可验证结果"], "nok", id="refusal-with-missing-frames"),
    pytest.param("nok", ["任务结果错误"], [], "nok", id="wrong-intent-or-image-delivery"),
    pytest.param("ok", [], [], "ok", id="valid-safety-refusal"),
    pytest.param("no_support", [], ["缺少前置条件"], "no_support", id="real-login-blocker"),
    pytest.param("no_support", [], ["待用户澄清"], "no_support", id="necessary-clarification"),
    pytest.param("others", [], ["未展示可验证结果"], "others", id="execution-unknown"),
    pytest.param("nok", [], ["未展示可验证结果"], "others", id="evidence-only-is-not-error"),
    pytest.param("others", [], ["环境或算力异常"], "others", id="temporary-message"),
    pytest.param("nok", ["执行中断"], [], "nok", id="fixed-failure-message"),
    pytest.param("nok", ["任务结果错误"], ["环境或算力异常"], "nok", id="error-before-temporary-failure"),
])
def test_new_verdict_categories_survive_parsing_aggregation_and_web_output(
    config, raw, issues, review, expected,
):
    skill = config.domain_skills["operation"]
    judge = SimpleNamespace(
        cfg=SimpleNamespace(name="judge_2", persona="终端用户"),
        expert_knowledge=config.expert_knowledge["operation"],
    )
    score = _operation_score_from_data(
        {"correctness": raw, "task_type": "simple", "issue_types": issues,
         "need_review_cate": review, "cards": [], "is_low_level": "no"},
        item_id="boundary-case", model_name="fake", judge=judge,
        dims=skill.rubrics, policy=skill.operation_policy,
        reply=SimpleNamespace(used_search=False, tool_trace=[], search_queries=[], truncated=False),
        latency=0,
    )
    assert score.correctness == expected
    verdict = aggregate_operation_scores(
        [score], skill.rubrics, config.ensemble, 0.6, skill.operation_policy.issue_types,
    )
    result = {}
    _fill_operation_verdict(result, verdict)
    assert result["judge_correctness"] == expected
    assert result["correctness"] == ("no_support" if expected == "others" else expected)
    if expected == "nok":
        assert result["issue_types"][0] == issues[0]
    else:
        assert result["issue_types"] == []
    assert result["need_review_cate"] == review
