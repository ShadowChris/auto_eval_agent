"""Task-only card scope, public columns and critical-fact review boundaries."""
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval.judges.ensemble import aggregate_operation_scores
from auto_eval.judges.operation_fields import (
    normalize_operation_cards, normalize_operation_fields, split_operation_issue_fields,
)
from auto_eval.schema import OperationSingleScore
from auto_eval.web.history import _with_operation_compat, export_rows
from auto_eval.web.runner import _fill_operation_verdict
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.prompts import OPERATION_SYSTEM


def config():
    return load_config(Path(__file__).resolve().parents[1] / "config")


def test_card_policy_keeps_location_and_storage_constraints_separate():
    cfg = config()
    skill = cfg.domain_skills['operation']
    prompt = OPERATION_SYSTEM.render(persona='终端用户', dims=skill.rubrics, scale=5,
        policy=skill.operation_policy, expert_knowledge_text=render_expert_knowledge(cfg.expert_knowledge['operation']))
    assert '临时浮层悬浮遮挡或压住原消息' in prompt
    assert '非挂卡的页面和弹窗仍是有效操作证据' in prompt
    assert '不得自行增加“先记录请求/临时保存”的目标' in prompt
    assert '用户明确允许临时保存或多处保存' in prompt
    assert '关键事实待核验' in prompt
    assert '不是所有图片都需复核' in prompt


def test_known_names_are_fixed_unknown_categories_are_short():
    cfg = config()
    catalog = {card.key: card.name for card in cfg.expert_knowledge['operation'].result_cards}
    cards = normalize_operation_cards([
        dict(type='calendar', observed_name='日程卡（已生成，周六提醒）'),
        dict(type='other', observed_name='航班卡（去香港，尚待用户选择）'),
        dict(type='other', observed_name='这是一张有很多内容以及附加说明的未知卡片'),
        *[dict(type=key, observed_name='不要这个自造名称') for key in ('image', 'video', 'huawei_video')],
    ], catalog)
    assert [card.observed_name for card in cards] == [
        '日程卡', '航班卡', '其他卡', '图片卡', '视频卡', '华为视频卡',
    ]
    assert [card.type for card in cards[-3:]] == ['image', 'video', 'huawei_video']
    aliases = normalize_operation_cards([
        dict(type='华为视频卡'), dict(type='other', observed_name='图片卡（手机图片）'),
    ], catalog)
    assert [card.type for card in aliases] == ['huawei_video', 'image']
    short_name = normalize_operation_cards([dict(type='unknown', observed_name='小艺帮记')], catalog)
    assert short_name[0].type == 'xiaoyi_memory'
    assert short_name[0].observed_name == '小艺帮记卡'


@pytest.mark.parametrize('raw', ['ok', 'nok', 'no_support', 'others'])
@pytest.mark.parametrize('independent_error', [False, True])
def test_fact_review_cannot_be_an_execution_failure_by_itself(raw, independent_error):
    policy = config().domain_skills['operation'].operation_policy
    issues = ['关键事实待核验', '内部过程信息泄露']
    if independent_error:
        issues.append('任务结果错误')
    status, issues, low_level = normalize_operation_fields(
        raw, issues, 'yes', 'complex', policy.issue_types,
    )
    assert status == ('nok' if independent_error else 'others')
    assert low_level == 'no'
    effect, review = split_operation_issue_fields(issues, policy.review_category_names)
    assert review == ['关键事实待核验']
    assert '关键事实待核验' not in effect
    assert ('任务结果错误' in effect) == independent_error


@pytest.mark.parametrize('states,expected_presence,expected_suitability', [
    ([], 'nok', ''),
    (['suitable', 'suitable'], 'ok', 'ok'),
    (['suitable', 'unclear'], 'ok', ''),
    (['unsuitable', 'unclear'], 'ok', 'nok'),
])
def test_card_summary_keeps_empty_unknown_and_decisive_wrong_distinct(
    states, expected_presence, expected_suitability,
):
    cfg = config()
    skill = cfg.domain_skills['operation']
    cards = normalize_operation_cards([
        dict(type='calendar', suitability=state, reason='简短依据', evidence_frames=[2])
        for state in states
    ], {'calendar': '日程卡'})
    score = OperationSingleScore(
        item_id='test', model='fake', judge='fake', correctness='ok',
        rubric={'操作完成度': 5, '步骤正确性': 5}, cards=cards,
    )
    verdict = aggregate_operation_scores([score], skill.rubrics, cfg.ensemble, .6, skill.operation_policy.issue_types)
    out = {}
    _fill_operation_verdict(out, verdict)
    assert out['card_presence'] == expected_presence
    assert out['card_suitability'] == expected_suitability
    assert out['card_types'] == (['日程卡'] if states else [])
    assert bool(out['card_suitability_reason']) == bool(states)
    assert out['correctness'] == 'ok'  # No card is not task failure.


def test_read_compat_does_not_rewrite_old_scope_or_other_modules():
    original = dict(mode='operation', items=[], results=[dict(
        correctness='ok', card_presence='present', card_types=['天气卡（杭州）'],
        card_suitability='unclear', cards=[dict(type='other', observed_name='旧弹窗')],
    )])
    current = _with_operation_compat(original)
    assert current['results'][0]['card_presence'] == 'ok'
    assert current['results'][0]['card_suitability'] == ''
    assert current['results'][0]['card_types'] == ['天气卡']
    assert current['results'][0]['cards'] == original['results'][0]['cards']
    assert original['results'][0]['card_presence'] == 'present'
    other = dict(original, mode='rich_content')
    assert _with_operation_compat(other) is other


def test_task_export_headers_change_without_new_json_fields():
    snapshot = dict(mode='operation', status='done', items=[dict(id='t', query='展示信息')],
        results=[dict(index=0, item_id='t', correctness='ok', issue_types=[],
            card_presence='ok', card_types=['图片卡'], card_suitability='',
            card_suitability_reason='关键身份待核验')])
    sheets = export_rows(snapshot)
    for sheet in ('评估结果', '逐题结果'):
        row = sheets[sheet][0]
        assert row['是否挂卡'] == 'ok'
        assert row['卡片类型'] == '图片卡'
        assert row['卡片合适判断'] == ''
        assert row['卡片合适判断原因'] == '关键身份待核验'
        assert 'card_presence' not in row
