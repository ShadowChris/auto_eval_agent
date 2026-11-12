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
    assert '实质影响任务完成时，才记任务结果错误' in prompt
    assert '不影响任务完成时不单独导致整体NOK' in prompt
    assert '合理近似不得放宽用户明确要求的精确条件' in prompt
    assert '也不能降为轻微质量问题或证据不足' not in prompt
    assert '一个平台替代多平台比较' in prompt
    assert prompt.index('4. 【已交付结果与卡片验收】') < prompt.index('5. 【阻塞资格表】')


def test_fact_review_only_covers_explicit_acceptance_requirements():
    prompt = operation_prompt()
    assert '当前不进行外部事实核验' in prompt
    assert '只有 Query 明确要求鉴定、辨认或核验身份、真实性等关键事实' in prompt
    assert '普通找图只点名对象不自动触发鉴定' in prompt
    assert '不将普通回答无来源转复核，不以无法核验判错' in prompt
    assert '已有实质影响任务完成的需求约束冲突或独立执行错误仍优先 nok' in prompt
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
    assert '所有任务先验收已给出的回答/方案' not in question
    prompt = operation_prompt()
    # Restored V6 flow includes the separate output rechecks, plus confirmed priors.
    assert len(prompt) < 25000
    assert '"need_review_cate"' in prompt
    assert 'decision_basis' not in prompt
    assert 'complex_082' not in prompt and '设置项1600条-1131' not in prompt


@pytest.mark.parametrize('multi_group', [False, True])
@pytest.mark.parametrize('has_query_images', [False, True])
def test_confirmed_product_endpoints_and_failure_priority_are_rendered(multi_group, has_query_images):
    """Exercise the actual model prompt for each supported rendering combination."""
    prompt = operation_prompt(multi_group=multi_group, has_query_images=has_query_images)
    assert '小艺帮记卡（类型 xiaoyi_memory）' in prompt
    assert '小艺帮记卡或保存确认中的蓝色“去查看”任意一个' in prompt
    assert '不能仅凭黑字覆盖卡片依据' in prompt
    assert '不要求同时出现，也不要求点击链接二次验证' in prompt
    assert '不能仅因只出现其中一种而判缺少可验证结果' in prompt
    assert '用户明确指定的应用、保存位置和能力要求仍须满足' in prompt
    assert '对应的小艺知识空间保存结果可验收小艺帮记目标' in prompt
    assert '名称差异不等于错应用或错位置' in prompt
    assert '主对话中对应本次保存内容的小艺帮记或小艺知识空间结果卡' in prompt
    assert '不能仅凭助手声称已保存认定完成' in prompt
    assert '也不泛化到其他应用或任意收藏功能' in prompt
    assert '生成Excel、PPT、Word等文档时' in prompt
    assert '生成中或已生成状态均可' in prompt
    assert '正文或预览不可见、没有下载链接或额外结果页' in prompt
    assert '其他明确目标如发送、发布分别验收' in prompt
    assert '与本次需求对应的文档生成卡本身就是该生成目标的完成证据，生成中或已生成均可' in prompt
    assert '不以完整标题、正文或下载入口作为识别文档卡的前提' in prompt
    assert '对应文档生成卡即使展示生成进度，仍按文档卡识别' in prompt
    assert '已识别的文档生成卡不因展示生成阶段而被排除' in prompt
    assert '卡片本身就是完成证据，不另索要结果证明' in prompt
    assert '专家经验可以帮助解释录屏，但不能代替任务完成证据' not in prompt
    assert '需求中点名对象不自动升级为鉴定任务' in prompt
    assert '明确要求鉴定、辨认或核验真伪时，返回图片本身不等于完成鉴定' in prompt
    assert '应核验能从图片中辨认的内容' in prompt
    assert '可见内容与明确要求存在实质冲突时，判 nok / 任务结果错误' in prompt
    assert '无法辨认的部分不猜测' in prompt
    assert '只有决定验收的要求确实无法判断，且无独立错误或有效阻塞时，判 others / 未展示可验证结果' in prompt
    assert '不能仅因无法精确鉴定排除图片证据，也不能仅因有图就忽略需求' in prompt
    assert '是否仍需用户授权或确认，按既有阻塞规则判断' in prompt
    assert '对操作影响或范围的必要确认，不等同于重复询问已经明确的用户原意' in prompt
    assert '任一存在就说明具备执行条件' not in prompt
    assert '用户明确指定的应用、平台、保存位置或交付渠道属于任务要求' in prompt
    assert '不能仅因替代结果用途相近，就降为轻微瑕疵' in prompt
    assert '不因中间过程未展示就推定使用了错误应用' in prompt
    assert '同一结果或后续交付足以证明多个目标时，可以共同使用' in prompt
    assert '不得仅凭执行顺序或其中一个目标完成，就推定其他目标完成' in prompt
    # Short summaries must not override the detailed image evidence rule.
    assert '不追加正文、精确鉴定或完整中间过程' not in prompt
    assert '不能因无法精确鉴定排除图片证据、转others' not in prompt
    assert '不维护无限别名列表' not in prompt
    # A card closes its target, but the existing fixed-failure exception remains.
    assert '最终必须判 nok / 执行中断' in prompt
    assert '不得因前面有完成声明、画面部分或全部目标已完成而跳过该映射' in prompt
    assert '“小艺算力不够了，请稍后再试”和“遇到一点小问题，请稍后再试”是 others' in prompt
    assert ('【原始用户输入图片】' in prompt) is has_query_images


@pytest.mark.parametrize('multi_group', [False, True])
def test_v6_flow_keeps_established_errors_before_blocker_explanations(multi_group):
    prompt = operation_prompt(multi_group=multi_group)
    output = prompt.split('【输出格式】', 1)[1]
    assert '候选执行问题核验' not in output
    assert '候选若仅是阻塞后果' not in output
    if multi_group:
        assert '【逐组目标状态】' in output
        assert '【逐组条件与候选错误】' not in output
        assert '先排除非结果卡、核验已交付结果，再核验阻塞' in output
        assert '不合并组间证据' in output
    else:
        assert output.index('4. 【已交付结果与卡片验收】') < output.index('5. 【阻塞资格表】')
        assert 'D.引用上一步已发生的问题，不重新判断或删除' in output
        assert '只根据上面的“已发生的问题”和“尚未交付原因”执行输出前反查' in output
        assert output.index('6. 【最终回答质量】') < output.index('7. 【最终结论】')
    assert '输出前反查 ok' in prompt
    assert '输出前反查 nok' in prompt
    assert '输出前反查 no_support / others' in prompt


def test_internal_uncertainty_and_blockers_remain_distinct_and_no_new_dimensions():
    cfg = load_config(Path(__file__).resolve().parents[1] / 'config')
    skill = cfg.domain_skills['operation']
    policy = skill.operation_policy
    assert list(policy.correctness) == ['ok', 'nok', 'no_support', 'others']
    assert '必要外部条件缺失' in policy.correctness['no_support']
    assert '现有证据不足' in policy.correctness['others']
    assert policy.issue_types['未展示可验证结果'].allowed_correctness == ['nok', 'others']
    assert policy.issue_types['待用户澄清'].allowed_correctness == ['nok', 'no_support']
    assert [(d.name, d.weight) for d in skill.rubrics] == [('操作完成度', 0.7), ('步骤正确性', 0.3)]
    prompt = operation_prompt()
    assert '缺少反证不等于完成' in prompt
    assert '只有一句完成声明且无其他依据的普通任务仍不足以判完成' in prompt
    # Declaring completion without sufficient evidence remains uncertainty, not a new NOK rule.
    assert '仅仅未展示可验证结果不属于可归责执行错误，应判 others' in prompt
    assert '合理近似不得放宽用户明确要求的精确条件' in prompt
