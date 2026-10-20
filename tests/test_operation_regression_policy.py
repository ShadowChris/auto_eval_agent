"""Task-only policy guards; real visual-effect regression is run separately."""
from pathlib import Path

from auto_eval.config import load_config
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.prompts import OPERATION_SYSTEM, RUBRIC_SYSTEM


def _prompt(**options) -> str:
    config = load_config(Path(__file__).resolve().parents[1] / 'config')
    skill = config.domain_skills['operation']
    return OPERATION_SYSTEM.render(
        persona='终端用户', dims=skill.rubrics, scale=5,
        policy=skill.operation_policy,
        expert_knowledge_text=render_expert_knowledge(config.expert_knowledge['operation']),
        **options,
    )


def test_operation_policy_checks_ui_semantics_before_result_mapping() -> None:
    prompt = _prompt()
    assert '默认值、历史搜索词、占位词、推荐词' in prompt
    assert '替代候选不等于 agent 已选错' in prompt
    assert '同一目标不能同时使用两者' in prompt
    assert '页面明确无匹配结果' in prompt


def test_operation_policy_does_not_invent_blockers_from_prerequisites() -> None:
    prompt = _prompt()
    assert '不能凭泛化失败自述推断该条件实际缺失' in prompt
    assert '默认支持范围不得扩大为所有第三方应用能力' in prompt
    assert '后续尚未开始的目标属于串行中断后果' in prompt
    assert 'A.用户必须补充的信息或动作' in prompt
    assert '只有 A/B/C 成立且 D 为否' in prompt


def test_operation_policy_preserves_exact_goal_constraints_and_unknown_conditions() -> None:
    prompt = _prompt()
    assert '不能用“核心诉求大致满足”省略明确要求' in prompt
    assert '一个平台替代多平台比较' in prompt
    assert '通用建议替代缺少个人信息的具体答案' in prompt
    assert '不能把“无法核验”解释成“核验结果为空”' in prompt


def test_operation_card_fitness_is_not_whole_task_completion() -> None:
    prompt = _prompt()
    assert '步骤/进度面板' in prompt
    assert '不能以 other/unclear 保留' in prompt
    assert '已完成/参考若干篇资料' in prompt
    assert '相关搜索/详情准备卡可为 supporting/suitable' in prompt
    assert '不证明后续播放、下载或预订完成' in prompt
    assert '仅凭日程标题就证明到点自动导航已配置' in prompt
    assert '相关但不能证明所需能力的卡片不得判 suitable' not in prompt


def test_blocker_qualification_precedes_missing_action_and_parallel_assumptions() -> None:
    prompt = _prompt()
    assert '最终文字询问本身就可证明待用户澄清' in prompt
    assert '可分别验收不等于必须并行执行' in prompt
    assert '仅因其他目标未展示结果，不能反推阻塞前已经漏做' in prompt
    assert '也不强制按 query 的语句顺序执行' in prompt
    assert '任务面板后续步骤文案和勾选不证明继续执行或阻塞解除' in prompt


def test_delivered_wrong_card_and_fixed_failure_cannot_be_hidden_by_blocker() -> None:
    prompt = _prompt()
    assert '替代必需结果的错卡是已交付错误' in prompt
    assert '即使同一回复前面声称已完成，也不能忽略或解释成正常结束话术' in prompt
    assert '查询本机或应用内的配对设备、记录、设置等真实状态仍适用查询证据规则' in prompt
    assert '只有“信息收集整理、处理任务、生成内容”等通用阶段的生成进度面板，不是文档结果卡' in prompt


def test_final_conclusion_rechecks_the_evidence_instead_of_washing_errors() -> None:
    prompt = _prompt()
    assert '助手回答区域重复出现同一自述仍是单一声明' in prompt
    assert '缺少最终结果是阻塞后果' in prompt
    assert '随后仍声称已经完成不是阻塞的必然后果' in prompt
    assert '系统内置信息、联系人等应用不属于第三方业务应用' in prompt
    assert '不能让结论与已经摘录的事实相矛盾' in prompt


def test_delivered_result_audit_precedes_blocker_and_quality_uses_bound_source() -> None:
    prompt = _prompt()
    assert prompt.index('4. 【已交付结果与卡片验收】') < prompt.index('5. 【阻塞资格表】')
    assert '错卡替代必需结果不属于“尚未交付”' in prompt
    assert '先锁定非空 answer，否则使用末段最终视觉回答' in prompt
    assert '必须引用该来源的泄露原句' in prompt
    assert '面向用户的产品、服务或协议名称本身不是内部调用过程' in prompt


def test_operation_prompt_stays_compact_and_question_policy_stays_separate() -> None:
    prompt = _prompt()
    assert len(prompt) < 22500
    assert '【Query 一致性门禁】' in prompt
    assert '"correctness": "ok|nok|no_support|others"' in prompt
    assert '默认值、历史搜索词、占位词、推荐词' not in RUBRIC_SYSTEM.render(
        persona='问答裁判', dims=[], scale=5,
    )


def test_product_endpoints_refine_existing_policy_without_new_modules() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / 'config')
    policy = config.domain_skills['operation'].operation_policy
    assert 'product_spec_rules' not in type(policy).model_fields
    assert 'product_acceptance_endpoints' not in {
        category.key for category in config.expert_knowledge['operation'].categories
    }
    prompt = _prompt()
    assert '结合原始意图与明确适用的产品规格确定验收终点' in policy.scope_rules[3]
    assert '按任务范围规则确定验收终点' in policy.decision_order[2]
    assert '页面交付例外仅限' not in prompt
    assert '页面交付目标按到达验收并评分，不追加后续动作' not in prompt
    assert '以目标应用的可操作页面交付' in prompt
    assert '恢复应用仅交付应用管理页面' in prompt
    assert '图片技能—导航类仅交付对应 App 页面' in prompt
    assert '不凭“导航”一词套用' in prompt
    assert '颜色不清且无独立错误判 others / 未展示可验证结果' in prompt
    assert '纯文本不能证明颜色，链接不计挂卡' in prompt
    assert '按已确定的验收终点判断' in policy.issue_types['应执行目标未执行'].description
    assert '不包括已实证达到验收终点' in policy.issue_types['应执行目标未执行'].description
    assert '按产品先验已交付页面' not in policy.issue_types['应执行目标未执行'].description
    assert '黑字判 nok / 任务结果错误' in prompt
    assert '保存确认' not in RUBRIC_SYSTEM.render(persona='问答裁判', dims=[], scale=5)


def test_page_delivery_exception_preserves_other_goal_and_evidence_boundaries() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / 'config')
    skill = config.domain_skills['operation']
    for multi_group in (False, True):
        prompt = OPERATION_SYSTEM.render(
            persona='终端用户', dims=skill.rubrics, scale=5,
            policy=skill.operation_policy,
            expert_knowledge_text=render_expert_knowledge(config.expert_knowledge['operation']),
            multi_group=multi_group,
        )
        assert '不将特定规格泛化到其他任务' in prompt
        assert '仅对验收终点要求实际提交、触发或状态生效的目标' in prompt
        # Ordinary entry requests remain valid; state changes and queries still need evidence.
        assert '可以按打开、查看或展示该功能入口理解' in prompt
        assert '必须观察实际状态满足要求' in prompt
        assert '仅“已设置/已查询”等完成声明不证明实际执行' in prompt
        assert '系统显示“√已完成”只表示本轮任务执行流程结束' in prompt
        assert '页面仅显示“搜索中/加载中”不能证明查询完成' in prompt
        # Narrowing the page exception does not change multi-goal or blocker handling.
        assert '每个生效目标都必须有对应证据' in prompt
        assert '通用建议替代缺少个人信息的具体答案' in prompt
        assert '串行后续未开始目标不单独记漏项' in prompt


def test_memory_confirmation_color_precedes_generic_missing_evidence() -> None:
    for multi_group in (False, True):
        prompt = _prompt(multi_group=multi_group)
        assert '小艺帮记保存确认出现“去查看”时，先看录屏颜色' in prompt
        assert '蓝字证明对应保存目标完成，不额外要求结果卡' in prompt
        assert '黑字判 nok / 任务结果错误，不被缺卡或其他目标缺证覆盖' in prompt
        assert '已辨黑字不得无新证据改称颜色不清' in prompt
        assert '颜色不清且无独立错误判 others / 未展示可验证结果' in prompt
        assert '不据此判复杂任务整体完成' in prompt
        assert '纯文本不能证明颜色，链接不计挂卡' in prompt
        assert '每个生效目标都必须有对应证据' in prompt


def test_memory_result_state_is_checked_before_generic_missing_evidence() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / 'config')
    policy = config.domain_skills['operation'].operation_policy
    evidence = '\n'.join(policy.evidence_rules)
    assert evidence.count('小艺帮记保存确认出现') == 1
    assert evidence.index('小艺帮记保存确认出现') < evidence.index('只有泛化的“正在操作/已完成”')
    assert '含保存确认、合格卡片' in policy.decision_order[4]
    assert '按专家经验核验状态与原目标约束' in policy.decision_order[4]


def test_verified_entry_completion_is_not_undone_by_returning() -> None:
    for multi_group in (False, True):
        prompt = _prompt(multi_group=multi_group)
        assert '功能入口目标未曾实证到达' in prompt
        assert '已达到验收终点且未被撤销，不因随后返回而改判未执行' in prompt
        assert '仅对验收终点要求实际提交、触发或状态生效的目标' in prompt
        assert '仅“已设置/已查询”等完成声明不证明实际执行' in prompt


def test_reply_conflict_requires_explicit_claim_instead_of_inferred_denial() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / 'config')
    policy = config.domain_skills['operation'].operation_policy
    description = policy.issue_types['回复与界面不一致'].description
    assert '明确事实断言与可见结果、对象或参数冲突' in description
    assert '必须引用冲突原句，否则不得标记' in description
    for multi_group in (False, True):
        prompt = _prompt(multi_group=multi_group)
        assert '不能把多余追问或未确认完成改写为否认已交付结果' in prompt
        assert '问句包含明确错误前提时仍须核验' in prompt
        assert '属于回复不够完整，不等于回复与界面冲突' in prompt
        assert '系统步骤不属于回复' in prompt
        assert '通用回复质量问题另记 issue，不改变执行根因' in prompt
        # Do not turn this clarification into an unconditional OK or blocker exemption.
        assert '不将特定规格泛化到其他任务' in prompt
        assert '串行后续未开始目标不单独记漏项' in prompt
        assert '录屏Query无法与输入Query一致核验' in prompt
        assert '最终必须判 nok / 执行中断' in prompt


def test_task_fact_summary_separates_results_claims_and_blocker_effects() -> None:
    prompt = _prompt()
    assert '目标与约束 | 实际业务结果及来源 | Agent 声明 | 系统步骤信息' in prompt
    assert '已交付正文/方案进入结果栏，完成声明只入声明栏' in prompt
    assert '正文不能证明操作已执行，但可直接验收内容约束' in prompt
    assert 'D.引用上一步已发生的问题，不重新判断或删除' in prompt
    assert '筛选范围或偏好不等于已选定唯一对象' in prompt
    assert '内容还不完整、其他动作尚待授权' in prompt
    assert '不从“页面已经打开、理论上能做、query 中排在前面”推断' in prompt
    assert 'complex_106' not in prompt
    assert 'goal_state_rules' not in prompt


def test_underspecified_settings_use_direct_entry_without_expanding_goal() -> None:
    prompt = _prompt()
    assert '参数欠指定设置入口规则' in prompt
    assert '目标状态、变化方向、约束或 agent 自主选择授权' in prompt
    assert '禁止使用入口例外' in prompt
    assert '不能仅因缺少精确数值而降格为打开页面' in prompt
    assert '上一级或错误页面不算到达' in prompt
    assert '不能仅凭页面有开启按钮就增加启用目标' in prompt
    assert '先记录验收方式（功能入口、状态变化、查询结果或内容交付）' in prompt
    assert '不按“设置”“可以”“能否”等单个词判动作' in prompt


def test_visible_query_results_do_not_require_new_ui_or_repeated_answer() -> None:
    prompt = _prompt()
    assert '不要求 Agent 新建证据' in prompt
    assert '不因还需阅读或滚动查看就要求另作文字复述' in prompt
    assert '部分列表不能证明全范围结论' in prompt
    assert '例如已连接图标不能单独证明全部网络服务正常' in prompt
    assert '仅文字声称“没有结果/找不到”而无查询证据时判 others' in prompt
    assert '不因为结果为空就额外要求用户创建、绑定或添加对象' in prompt


def test_feature_configuration_panels_are_not_saved_content_cards() -> None:
    assert '设置弹窗（即使覆盖主对话）' in _prompt()
    assert '非挂卡的页面和弹窗仍是有效操作证据' in _prompt()


def test_decision_priority_precedes_details_without_case_specific_rules() -> None:
    prompt = _prompt()
    assert prompt.index('【判定顺序】') < prompt.index('【任务范围】')
    assert '不能用部分完成加有效阻塞判 ok' in prompt
    assert '不能用仅有文字声明和流程已完成判本机状态查询 ok' in prompt
    assert '输出前反查 ok' in prompt and '输出前反查 nok' in prompt
    assert '输出前反查 no_support / others' in prompt
    assert 'complex_106' not in prompt and '设置项1600条-0524' not in prompt


def test_blocked_serial_targets_are_not_logically_independent_errors() -> None:
    prompt = _prompt()
    assert '不是“逻辑上可独立完成的目标尚未开始”' in prompt
    assert '不因其他目标理论上可独立执行而判 nok' in prompt
    assert '不能只写另一个目标本来可独立完成' in prompt
    assert '只有实际错操作、错误交付、明确失败' in prompt


def test_card_claims_cannot_supply_missing_capabilities_or_result_qualification() -> None:
    prompt = _prompt()
    assert 'agent 的文字承诺不能补足卡片未证明的额外能力或动作' in prompt
    assert '原生APP页面与设置弹窗只作为操作证据' in prompt
    assert 'cards 中替代必需结果的 direct/unsuitable' in prompt
    assert '整体不能同时解释为纯阻塞' in prompt
