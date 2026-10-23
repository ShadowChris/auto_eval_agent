"""Policy wiring guards, not a substitute for real visual backtests."""
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.prompts import OPERATION_SYSTEM, RUBRIC_SYSTEM


def operation_prompt(**options):
    cfg = load_config(Path(__file__).resolve().parents[1] / 'config')
    skill = cfg.domain_skills['operation']
    return OPERATION_SYSTEM.render(
        persona='终端用户', dims=skill.rubrics, scale=5,
        policy=skill.operation_policy,
        expert_knowledge_text=render_expert_knowledge(cfg.expert_knowledge['operation']),
        **options,
    )


@pytest.mark.parametrize('multi_group', [False, True])
def test_content_delivery_and_action_claims_have_separate_sources(multi_group):
    prompt = operation_prompt(multi_group=multi_group)
    assert '已交付正文/方案进入结果栏，完成声明只入声明栏' in prompt
    assert '助手回答即使出现在画面里也只填声明列' not in prompt
    assert '仅“已设置/已查询”等完成声明不证明实际执行' in prompt
    assert '真实状态仍适用查询证据规则' in prompt


def test_direct_constraint_conflict_is_not_external_fact_checking():
    prompt = operation_prompt()
    assert '不核查外部真实性' in prompt
    assert '已给出的回答/方案也须验收' in prompt
    assert '本机状态查询的答复也要检查' in prompt
    assert '可直接核对的约束冲突是任务结果错误' in prompt
    assert '也不能降为轻微质量问题或证据不足' in prompt
    assert '一个平台替代多平台比较' in prompt
    assert prompt.index('4. 【阻塞资格表】') < prompt.index('5. 【已交付结果与卡片验收】')


def test_fact_review_only_covers_explicit_acceptance_requirements():
    prompt = operation_prompt()
    assert '当前不进行外部事实核验' in prompt
    assert '只有 Query 明确要求的身份、真实性等关键事实决定验收' in prompt
    assert '不将普通回答无来源转复核，不以无法核验判错' in prompt
    assert '已有可验证的需求约束冲突或独立执行错误仍优先 nok' in prompt
    cfg = load_config(Path(__file__).resolve().parents[1] / 'config')
    policy = cfg.domain_skills['operation'].operation_policy
    assert policy.issue_types['关键事实待核验'].allowed_correctness == ['nok', 'others']
    assert policy.review_category_names.count('关键事实待核验') == 1


def test_review_scope_does_not_replace_visual_action_evidence_or_card_scope():
    prompt = operation_prompt(has_query_images=True)
    assert '用户输入图片是任务输入' in prompt
    assert '不主动把所有图片' not in prompt  # Replaced by the explicit-query guard.
    assert '不是所有图片都需复核' in prompt
    assert '设置弹窗（即使覆盖主对话）' in prompt
    assert '替代必需结果的错卡是已交付错误' in prompt
    assert '参数欠指定设置入口规则' in prompt


def test_operation_boundary_does_not_enter_question_prompt_or_grow_a_new_field():
    question = RUBRIC_SYSTEM.render(persona='问答裁判', dims=[], scale=5)
    assert '关键事实待核验' not in question
    assert '已给出的回答/方案也须验收' not in question
    prompt = operation_prompt()
    assert len(prompt) < 22500
    assert '"need_review_cate"' in prompt
    assert 'decision_basis' not in prompt
    assert 'complex_082' not in prompt and '设置项1600条-1131' not in prompt
