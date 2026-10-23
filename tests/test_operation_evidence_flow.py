"""Prompt flow contracts only; real visual accuracy is assessed by separate model runs."""
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.prompts import OPERATION_SYSTEM


@pytest.mark.parametrize('multi_group', [False, True])
@pytest.mark.parametrize('has_query_images', [False, True])
def test_fact_blocker_candidate_flow_is_delivered_to_all_task_modes(
    multi_group, has_query_images,
):
    cfg = load_config(Path(__file__).resolve().parents[1] / 'config')
    skill = cfg.domain_skills['operation']
    policy = skill.operation_policy
    assert len(policy.decision_order) == 6
    assert 'Query 一致性门禁' in policy.decision_order[0]
    assert '各目标验收终点及必要输入' in policy.decision_order[1]
    assert '建立事实摘要' in policy.decision_order[2]
    assert '先识别必要阻塞' in policy.decision_order[3]
    assert '再核验候选错误' in policy.decision_order[4]
    assert '整体映射' in policy.decision_order[5]
    prompt = OPERATION_SYSTEM.render(
        persona='终端用户', dims=skill.rubrics, scale=5,
        policy=policy,
        expert_knowledge_text=render_expert_knowledge(cfg.expert_knowledge['operation']),
        multi_group=multi_group, has_query_images=has_query_images,
    )
    for boundary in (
        '普通可操作开关未开启不自动满足 B',
        '推测不证明本次条件确实缺失',
        '有结果与无记录使用同一证据门槛',
        '配置控件关闭不证明内容保存失败',
        '不能以 other/unclear 保留',
        '不得把问句意译成原文没有表达的否认',
        '候选若仅由流程、自述、配置开关或阻塞后果推断，必须撤销',
        '已有实际错操作、错误交付、明确失败',
        '一律判 others / 未展示可验证结果，不要求先证明曾尝试',
    ):
        assert boundary in prompt
    assert 'D.引用上一步已发生的问题，不重新判断或删除' not in prompt
    assert '均属于播放目标未执行' not in policy.issue_types['应执行目标未执行'].description
    assert len(prompt) < 22500 + (600 if has_query_images else 0)
