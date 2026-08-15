from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_eval.config import load_config
from auto_eval.expert_knowledge import render_expert_knowledge
from auto_eval.judges.operation_fields import (
    hoist_misnested_operation_fields,
    map_legacy_operation_result,
    normalize_operation_fields,
)
from auto_eval.judges.prompts import ARBITRATOR_SYSTEM, OPERATION_SYSTEM, OPERATION_USER
from auto_eval.judges.rubric_judge import _flatten_rubric
from auto_eval.schema import OperationSingleScore


def _operation_prompt():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    config = load_config(config_dir)
    operation = config.domain_skills["operation"]
    prompt = OPERATION_SYSTEM.render(
        persona="测试裁判",
        dims=operation.rubrics,
        scale=5,
        policy=operation.operation_policy,
        expert_knowledge_text=render_expert_knowledge(
            config.expert_knowledge["operation"]
        ),
    )
    return operation, prompt


def test_operation_policy_and_dimensions_load_from_yaml() -> None:
    operation, prompt = _operation_prompt()
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert [dim.name for dim in operation.rubrics] == ["操作完成度", "步骤正确性"]
    assert [dim.weight for dim in operation.rubrics] == [0.7, 0.3]
    assert operation.operation_policy is not None
    assert config.expert_knowledge["operation"].version == 3
    assert config.expert_knowledge["operation"].categories
    assert operation.operation_policy.scope_rules
    assert operation.operation_policy.evidence_rules
    assert list(operation.operation_policy.correctness) == [
        "ok",
        "nok",
        "no_support",
        "others",
    ]
    assert operation.operation_policy.issue_types["内部过程信息泄露"].allowed_correctness == [
        "ok",
        "nok",
        "no_support",
        "others",
    ]
    assert operation.operation_policy.issue_types[
        "录屏Query无法与输入Query一致核验"
    ].allowed_correctness == ["others"]
    assert operation.rubrics[0].score_anchors[5].startswith(
        "整个 query 的所有生效目标完整闭环"
    )
    assert "1. 操作完成度（权重 0.7，1–5 分）" in prompt
    assert "2. 步骤正确性（权重 0.3，1–5 分）" in prompt


def test_operation_prompt_uses_new_whole_query_decision_policy() -> None:
    _, prompt = _operation_prompt()

    assert "基于整个 query 评估，不要拆成多个独立 case" in prompt
    assert "简单任务只有一个可独立验收的最终结果" in prompt
    assert "复杂多任务包含多个可独立验收的结果" in prompt
    assert "前置判断结果和条件成立后的执行动作" in prompt
    assert "- ok：" in prompt
    assert "- nok：" in prompt
    assert "- no_support：" in prompt
    assert "- others：" in prompt
    assert "不存在优先级更高的可归责执行错误" in prompt
    assert "已观察到的结果性执行错误优先于后续的临时异常或能力不支持声明" in prompt
    assert "未预期场景" in prompt
    assert "其他未归类情况" in prompt
    assert "所有生效目标完成或正确跳过时判 ok" in prompt
    assert '"correctness": "ok|nok|no_support|others"' in prompt
    assert '"correctness": "right|wrong|partial|unclear"' not in prompt


def test_operation_prompt_applies_query_alignment_gate_before_execution() -> None:
    _, prompt = _operation_prompt()

    assert "【Query 一致性门禁】" in prompt
    assert "录屏Query无法与输入Query一致核验" in prompt
    assert "未展示完整、被遮挡、模糊、无法辨认" in prompt
    assert "不进行同义改写推断" in prompt
    assert "任务执行面板的规划或步骤、工具调用文字、agent 回复都不能代替" in prompt
    assert "所有维度和总分填 null" in prompt
    assert "停止后续任务执行评判" in prompt


def test_operation_prompt_keeps_response_quality_issues_separate_from_correctness() -> None:
    operation, prompt = _operation_prompt()

    assert "内部过程信息泄露" in prompt
    assert "回复语义重复" in prompt
    assert "回复内容自相矛盾" in prompt
    assert "重复系统卡片" in prompt
    assert "通用质量问题不能作为 nok、no_support、others 的第一项" in prompt
    assert "默认不直接改变 correctness" in prompt
    assert "必须再独立通读完整 agent 回复" in prompt
    assert "我先想想任务规划" in prompt
    assert "严重回复质量问题" not in operation.operation_policy.issue_types


def test_operation_prompt_handles_conditional_tasks_and_causality() -> None:
    _, prompt = _operation_prompt()

    assert "只要求完成条件实际成立后所激活的目标" in prompt
    assert "条件不成立时正确跳过后续动作，也属于完成" in prompt
    assert "已获得前置信息但条件判断错误" in prompt
    assert "条件成立却未执行后续动作" in prompt
    assert "对每个目标优先识别最直接根因" in prompt
    assert "同一个目标不要同时记录根因和其必然后果" in prompt
    assert "优先标记三方应用跳转中断" in prompt
    assert "不能覆盖已经发生的错误路径、无关操作或执行失败" in prompt
    assert "所有未完成目标都被外部条件阻塞时判 no_support" in prompt


def test_operation_output_hoists_fields_accidentally_nested_in_rubric() -> None:
    data = {
        "task_type": "complex",
        "rubric": {
            "操作完成度": {"total": 2, "reason": "仅完成一个目标"},
            "步骤正确性": {"total": 3, "reason": "另一目标未执行"},
            "total": 2.3,
            "correctness": "nok",
            "issue_types": ["应执行目标未执行"],
            "is_low_level": "no",
            "rationale": "存在独立的可归责错误。",
        },
    }

    normalized = hoist_misnested_operation_fields(data)

    assert normalized["correctness"] == "nok"
    assert normalized["issue_types"] == ["应执行目标未执行"]
    assert normalized["total"] == 2.3
    assert set(normalized["rubric"]) == {"操作完成度", "步骤正确性"}


def test_operation_prompt_distinguishes_state_setting_from_action_prerequisite() -> None:
    _, prompt = _operation_prompt()

    assert "区分幂等状态设置和依赖活动对象的动作指令" in prompt
    assert "停止播放、暂停下载、挂断电话、取消导航" in prompt
    assert "活动对象不存在时不能按“初始状态已满足”处理" in prompt
    assert "动作没有对应活动对象" in prompt
    assert "判 no_support，并标记缺少前置条件" in prompt


def test_operation_prompt_judges_evidence_by_sufficiency_not_container() -> None:
    _, prompt = _operation_prompt()

    assert "【证据规则】" in prompt
    assert "取决于其展示的具体内容，而不是所在载体" in prompt
    assert "设置结果卡片直接显示目标开关状态或当前值" in prompt
    assert "可以形成充分的完成证据链" in prompt
    assert "每个生效目标都必须有对应证据" in prompt
    assert "一个目标的证据不能替代其他目标" in prompt
    assert "缺少反证不等于存在完成证据" in prompt
    assert "不得因其他目标执行成功" in prompt
    assert "只有泛化的“正在操作/已完成”" in prompt
    assert "只能证明尝试" in prompt
    assert "严格区分“目标未执行”和“执行后缺少结果证据”" in prompt
    assert "同一目标不能同时使用两者" in prompt
    assert "该状态与 query 要求不一致时，优先标记任务结果错误" in prompt
    assert "没有展示可判断对错的具体结果状态时" in prompt
    assert "最终画面或初始状态直接满足 query 即可" in prompt
    assert "关键帧未展示全部过渡过程不等于步骤错误" in prompt
    assert "将每个生效目标归入：已完成、等待用户、缺少前置条件" in prompt
    assert "不主动进行外部事实核验" in prompt
    assert "是否与录屏、结果卡、上下文或可信先验存在明确冲突" in prompt
    assert "自动操作卡片、进度、目标入口和操作轨迹属于过程证据" not in prompt
    assert "ok 必须有最终状态强证据" not in prompt


def test_operation_prompt_separates_collapsed_window_from_plain_text_claim() -> None:
    _, prompt = _operation_prompt()

    assert "已结束操控，点击查看" in prompt
    assert "任务执行窗口始终处于带“查看/点击查看”入口的缩略状态" in prompt
    assert "判 others，优先标记任务执行窗口未展开" in prompt
    assert "相关评分维度填 null" in prompt
    assert "模型收到的画面不足以验证结果" in prompt
    assert "判 others" in prompt
    assert "标记未展示可验证结果" in prompt
    assert "不得推测是否由抽帧遗漏造成" in prompt
    assert "任务执行窗口未展开" in prompt


def test_operation_prompt_ignores_recording_infrastructure() -> None:
    operation, prompt = _operation_prompt()

    assert "【专家经验】" in prompt
    assert "判断能力范围时，专家经验优先于 Agent 自述" in prompt
    assert "判断本次执行状态时，以录屏中的直接证据为准" in prompt
    assert "证书与凭据" in prompt
    assert "来电播报功能" in prompt
    assert "始终播报" in prompt
    assert "仅耳机" in prompt
    assert "耳机与汽车" in prompt
    assert "不播报" in prompt
    assert "来自评测录屏工具，不是 agent 操作" in prompt
    assert "顶部状态栏或灵动岛的红点和计时不能证明相机正在录像" in prompt
    assert "相机应用内部的停止或暂停按钮" in prompt
    assert not any(
        "录屏工具自身的计时" in criterion
        for criterion in operation.rubrics[1].criteria
    )
    assert "【录屏载体噪声】" not in prompt


def test_operation_prompt_injects_completion_marker_and_podcast_knowledge() -> None:
    _, prompt = _operation_prompt()

    assert "“√已完成”只表示本轮任务执行流程结束" in prompt
    assert "不得仅凭该标识判为 ok" in prompt
    assert "该标识是系统流程状态，不是 agent 的自然语言回复" in prompt
    assert "不得仅因它与实际任务结果不同而标记回复与界面不一致" in prompt
    assert "任务执行面板中的步骤文案、勾选标记" in prompt
    assert "步骤显示“已完成”只证明该步骤结束" in prompt
    assert "不能用于标记回复与界面不一致或回复内容自相矛盾" in prompt
    assert "同一面板另有“当前操作需要你的确认”" in prompt
    assert "随后出现的超时终止不改变这一根因" in prompt
    assert "出现可识别的播客系统结果卡" in prompt
    assert "卡片仍显示“生成中”或尚未自动播放" in prompt
    assert "与卡片仍在生成具体音频内容不构成回复与界面不一致" in prompt
    assert "没有播客结果卡时，不能判完成" in prompt


def test_operation_prompt_maps_fixed_failure_messages() -> None:
    operation, prompt = _operation_prompt()

    assert "无法为你继续操作了" in prompt
    assert "小艺算力不够了，请稍后再试" in prompt
    assert "遇到一点小问题，请稍后再试" in prompt
    assert "“无验证结果”映射为 others / 未展示可验证结果" in prompt
    assert "仅仅未展示可验证结果不属于可归责执行错误" in prompt
    assert operation.operation_policy.issue_types["未展示可验证结果"].allowed_correctness == [
        "nok",
        "others",
    ]
    assert "结果验证异常" not in operation.operation_policy.issue_types


def test_operation_prompt_injects_torch_and_document_generation_knowledge() -> None:
    _, prompt = _operation_prompt()

    assert "Excel 等文档生成任务耗时较长" in prompt
    assert "文档类型和内容主题相符" in prompt
    assert "只有泛化的“正在生成”文字而没有文档结果卡时不能判完成" in prompt
    assert "手电筒开启后" in prompt
    assert "手电筒图标元素" in prompt
    assert "右侧会显示“已开启”" in prompt
    assert "可以作为手电筒已经开启的直接结果证据" in prompt


def test_operation_prompt_distinguishes_guided_user_wait_from_silent_stall() -> None:
    _, prompt = _operation_prompt()

    assert "对未完成目标必须识别其最后有效状态" in prompt
    assert "界面、任务执行过程文字或 agent 最终回答明确提示用户登录、授权、验证" in prompt
    assert "没有任何用户指引、最终回答为空且流程停止或超时" in prompt
    assert "只有同时满足以下条件时才判 nok / 三方应用跳转中断" in prompt
    assert "明确反馈本次任务已因等待而超时、终止" in prompt
    assert "界面、任务执行文字和 agent 均无任何澄清或用户操作提示" in prompt
    assert "agent 最终回答为空" in prompt
    assert "没有因等待用户而超时或终止的反馈" in prompt
    assert "回复非空不自动等于 no_support" in prompt
    assert "缺少用户后续操作和最终结果只是阻塞的必然后果" in prompt
    assert "已经明确等待用户的目标，不得再因用户未响应" in prompt
    assert "agent 明确说明助手、设备或系统不支持" in prompt
    assert "没有可信专家经验或可见证据反驳" in prompt
    assert "必须同时检查按时间顺序的视觉证据和 agent 文本自述" in prompt
    assert "不得只依据其中一路下结论" in prompt
    assert "任务执行过程文字或 agent 最终回答" in prompt
    assert "返回桌面或助手界面" in prompt
    assert "缺少完成任务所需的信息并指引用户提供" in prompt
    assert "泛化能力常识、相似能力或裁判设想的替代策略" in prompt
    assert "判 no_support 前必须逐项写明" in prompt
    assert "先发生可见错误后再询问用户换路径" in prompt
    assert "用户实际答复、选择、确认、完成登录授权" in prompt
    assert "通常可以通过其他网络、应用、入口或方法完成" in prompt
    assert "不强制要求 agent 再重复说明" in prompt
    assert "仅返回桌面或助手界面不算解除" in prompt
    assert "无法继续”“请自行操作”“相关工具已返回结果" in prompt


def test_operation_prompt_does_not_expand_goal_or_treat_recovery_question_as_blocker() -> None:
    _, prompt = _operation_prompt()

    assert "不得擅自扩大 query 的完成边界" in prompt
    assert "可以按打开、查看或展示该功能入口理解" in prompt
    assert "query 已提供继续执行所需信息" in prompt
    assert "只是错误后的恢复询问" in prompt
    assert "不能把此前可见错误改写为等待用户" in prompt
    assert "专家经验已确认能力或页面存在时" in prompt
    assert "用户不负责选择 agent 的搜索、导航、工具或补救策略" in prompt
    assert "需要我继续到设置中查找吗" in prompt
    assert "换关键词、换入口还是上报" in prompt


def test_operation_prompt_requires_complete_blocker_qualification() -> None:
    _, prompt = _operation_prompt()

    assert "【阻塞资格表】" in prompt
    assert "用户必须补充的信息或完成的具体动作" in prompt
    assert "最后有效状态中该阻塞是否仍未解除" in prompt
    assert "只有 A/B/C 成立且 D 为否" in prompt


def test_operation_prompt_rechecks_task_after_blocker_is_released() -> None:
    _, prompt = _operation_prompt()

    assert "应用内容、搜索结果、商品详情" in prompt
    assert "旧阻塞立即解除" in prompt
    assert "解除后必须重新评价剩余目标" in prompt
    assert "即使后续执行错误或不完整" in prompt


def test_operation_prompt_distinguishes_empty_query_result_from_missing_action_object() -> None:
    _, prompt = _operation_prompt()

    assert "区分“查询结果为空”和“动作缺少目标对象”" in prompt
    assert "未找到、无记录、暂无结果" in prompt
    assert "没有可恢复应用、目标文件、剪贴板内容或活动对象" in prompt
    assert "不能因为成功进入页面或展示空状态就判 ok" in prompt


def test_operation_prompt_models_shared_clarification_dependencies() -> None:
    _, prompt = _operation_prompt()

    assert "多个目标共享同一个必须由用户补充" in prompt
    assert "不要求 agent 在询问前先打开应用" in prompt
    assert "自动接听、日程、联系人或地址、机票酒店门票" in prompt
    assert "合理搜索未找到 query 指定对象后" in prompt
    assert "不相关关键词" in prompt
    assert "按 no_support / 待用户澄清处理" in prompt


def test_operation_prompt_does_not_infer_missing_object_from_partial_list() -> None:
    _, prompt = _operation_prompt()

    assert "当前画面只展示列表的一部分" in prompt
    assert "不足以证明该对象客观不存在" in prompt
    assert "完整搜索后明确显示无结果" in prompt


def test_operation_prompt_keeps_action_task_open_while_waiting_for_user() -> None:
    _, prompt = _operation_prompt()

    assert "原始任务的完成边界" in prompt
    assert "预订、下单、发送、发布等实际动作" in prompt
    assert "前置查询结果只是执行过程" in prompt
    assert "不能提前判 ok" in prompt
    assert "即使前置查询、搜索、导航到目标页或内容准备等步骤已经正确完成" in prompt
    assert "只要原始任务仍需用户答复后才能继续" in prompt
    assert "按时间顺序检查全部关键帧" in prompt
    assert "通读完整 agent 自述" in prompt
    assert "已完成、等待用户、缺少前置条件、静默停滞" in prompt
    assert "阻塞前是否已有足以独立导致失败的可见错误" in prompt
    assert "禁止把任务面板的步骤、“已完成”或“超时，任务终止”当成 agent 回复" in prompt
    assert "必须引用冲突的具体 agent 自然语言回复" in prompt


def test_operation_prompt_requires_blocker_evidence_gate_before_correctness() -> None:
    _, prompt = _operation_prompt()

    assert "必须先完成“阻塞证据门控”" in prompt
    assert "逐帧扫描并逐字摘录面向用户的登录、授权、同意" in prompt
    assert "协议说明配合“取消/同意”等按钮" in prompt
    assert "从完整 agent 文本中逐字摘录“无法/缺少/未提供/请提供" in prompt
    assert "阻塞证据门控的映射是强约束" in prompt
    assert "【视觉指引扫描】" in prompt
    assert "【文本指引扫描】" in prompt
    assert "【阻塞资格表】" in prompt
    assert "【时序与强制映射】" in prompt
    assert "只有能够逐字引用的完整句子或协议提示" in prompt
    assert "普通“登录”入口、“查看”按钮、“手动操作中”状态" in prompt
    assert "泛化终止语只有在同时说明具体能力限制" in prompt
    assert "不得意译或补写原文中没有的询问" in prompt


def test_operation_prompt_follows_serial_execution_chain_and_requires_direct_playback_evidence() -> None:
    _, prompt = _operation_prompt()

    assert "复杂多任务按实际串行执行链判断" in prompt
    assert "后续尚未开始的目标属于串行中断后果" in prompt
    assert "即使这些目标逻辑上不依赖同一信息" in prompt
    assert "阻塞前已经发生可见错误" in prompt
    assert "搜索结果卡、影视详情卡、播放按钮" in prompt
    assert "不能证明播放动作已经发生" in prompt
    assert "普通应用跳转、打开视频应用、模糊的应用加载画面" in prompt
    assert "播放专属的直接信号" in prompt
    assert "正在播放/播放中" in prompt
    assert "不能仅因画面跳转或模糊而降级为 others" in prompt
    assert "判 nok / 应执行目标未执行" in prompt
    assert "已经观察到上述播放专属启动信号" in prompt
    assert "【结果证据专项】" in prompt


def test_operation_prompt_distinguishes_action_preparation_from_core_trigger() -> None:
    _, prompt = _operation_prompt()

    assert "不可省略的核心提交或触发动作" in prompt
    assert "必须区分前置准备和核心动作执行" in prompt
    assert "进入对应应用、跳转目标页面、找到目标对象" in prompt
    assert "不能证明核心动作已经执行" in prompt
    assert "目标专属的直接触发、执行中或执行完成反馈" in prompt
    assert "普通应用跳转或模糊的应用加载画面" in prompt


def test_operation_prompt_uses_explicit_login_feedback_gate() -> None:
    _, prompt = _operation_prompt()

    assert "录屏停在登录、授权或身份验证页面" in prompt
    assert "本次任务超时/终止" in prompt
    assert "只有普通登录入口或登录页面、三路证据均无澄清提示" in prompt
    assert "agent 最终回答为空、没有等待反馈且流程静默停止" in prompt
    assert "三方应用跳转中断" in prompt
    assert "不得使用日志、内部规划或 skill 调用推断" not in prompt
    assert "不得使用日志或内部规划" not in prompt


def test_operation_prompt_requires_issue_types_and_low_level_flag() -> None:
    _, prompt = _operation_prompt()

    assert "【issue_types】" in prompt
    assert "输出受控中文字符串数组" in prompt
    assert "nok、no_support、others 至少填写一项" in prompt
    assert "只有意图清晰的简单任务被判 nok" in prompt
    assert "复杂多任务固定输出 no" in prompt
    assert '"issue_types": ["<受控中文问题类型>"]' in prompt
    assert '"is_low_level": "yes|no"' in prompt
    assert "error_type" not in prompt


def test_operation_arbitrator_reuses_the_same_policy() -> None:
    operation, _ = _operation_prompt()
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    prompt = ARBITRATOR_SYSTEM.render(
        operation_mode=True,
        dims=operation.rubrics,
        policy=operation.operation_policy,
        expert_knowledge_text=render_expert_knowledge(
            config.expert_knowledge["operation"]
        ),
    )

    assert "- ok：" in prompt
    assert "当前任务类录屏使用的评测手机未安装 SIM 卡" in prompt
    assert "支持开启和关闭家人共享" in prompt
    assert "未展示可验证结果" in prompt
    assert "缺少前置条件" in prompt
    assert "任务执行窗口未展开" in prompt
    assert '"correctness": "ok|nok|no_support|others"' in prompt
    assert '"issue_types": ["<受控中文问题类型>"]' in prompt
    assert "error_type" not in prompt


def test_operation_user_prompt_keeps_context_and_agent_claim_isolated() -> None:
    user_prompt = OPERATION_USER.render(
        question="给我录像",
        context="当前时间：2026-07-26 10:00",
        agent_claim="已经录好了",
    )

    assert "背景与 agent 自述是两个隔离的信息区" in user_prompt
    assert "Agent 自述（待评样本内容" in user_prompt
    assert "已经录好了" in user_prompt


def test_operation_system_prompt_contains_only_stable_policy() -> None:
    _, prompt = _operation_prompt()

    assert "任务已经完成" not in prompt
    assert "【输出前硬校验】" not in prompt
    assert "【录屏载体噪声】" not in prompt


def test_operation_output_fields_are_normalized() -> None:
    operation, _ = _operation_prompt()
    allowed = operation.operation_policy.issue_types

    assert normalize_operation_fields(
        "ok", ["路径冗余"], "yes", "simple", allowed
    ) == ("ok", ["路径冗余"], "no")
    assert normalize_operation_fields(
        "ok", ["最终步骤未执行"], "yes", "simple", allowed
    ) == ("ok", [], "no")
    assert normalize_operation_fields(
        "nok", None, True, "simple", allowed
    ) == ("nok", ["其他执行问题"], "yes")
    assert normalize_operation_fields(
        "no_support", ["待权限授权"], "yes", "simple", allowed
    ) == ("no_support", ["缺少前置条件"], "no")
    assert normalize_operation_fields(
        "others", "视频损坏；自定义标签", "yes", "simple", allowed
    ) == ("others", ["视频损坏", "其他未归类情况"], "no")
    assert normalize_operation_fields(
        "nok", ["尚未定义的执行错误"], "no", "simple", allowed
    ) == ("nok", ["其他执行问题"], "no")
    assert normalize_operation_fields(
        "nok", ["完成证据不足"], "yes", "complex", allowed
    ) == ("others", ["未展示可验证结果"], "no")
    assert normalize_operation_fields(
        "nok", ["应执行目标未执行", "完成证据不足"], "yes", "complex", allowed
    ) == ("nok", ["应执行目标未执行", "未展示可验证结果"], "no")
    assert normalize_operation_fields(
        "ok", ["内部过程信息泄露"], "yes", "simple", allowed
    ) == ("ok", ["内部过程信息泄露"], "no")
    assert normalize_operation_fields(
        "nok",
        ["缺少前置条件", "应执行目标未执行", "内部过程信息泄露"],
        "yes",
        "complex",
        allowed,
    ) == (
        "nok",
        ["应执行目标未执行", "缺少前置条件", "内部过程信息泄露"],
        "no",
    )
    assert normalize_operation_fields(
        "no_support",
        ["内部过程信息泄露", "待用户澄清"],
        "yes",
        "simple",
        allowed,
    ) == ("no_support", ["待用户澄清", "内部过程信息泄露"], "no")
    assert normalize_operation_fields(
        "nok",
        ["录屏Query未完整展示", "应执行目标未执行"],
        "yes",
        "simple",
        allowed,
    ) == ("others", ["录屏Query无法与输入Query一致核验"], "no")


def test_legacy_operation_results_map_at_read_time() -> None:
    assert map_legacy_operation_result("right", None) == ("ok", [])
    assert map_legacy_operation_result("partial", "路径冗余") == (
        "ok",
        ["路径冗余"],
    )
    assert map_legacy_operation_result("wrong", "仅文字无状态证据") == (
        "others",
        ["未展示可验证结果"],
    )
    assert map_legacy_operation_result("unclear", "待权限授权") == (
        "no_support",
        ["缺少前置条件"],
    )
    assert map_legacy_operation_result("unclear", "录屏证据缺失") == (
        "others",
        ["录屏数据不完整"],
    )


def test_operation_schema_requires_issue_types_for_non_ok() -> None:
    OperationSingleScore(
        item_id="i",
        model="m",
        judge="j",
        correctness="ok",
        issue_types=[],
    )
    with pytest.raises(ValidationError):
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j",
            correctness="nok",
            issue_types=[],
        )


def test_operation_total_reason_output_supports_na() -> None:
    rubric, reasons, na_dimensions = _flatten_rubric(
        {
            "操作完成度": None,
            "步骤正确性": {
                "total": 4,
                "reason": "正确识别客观阻塞并停止",
            },
        },
        dim_names=["操作完成度", "步骤正确性"],
    )

    assert rubric == {"步骤正确性": 4}
    assert reasons == {"步骤正确性": "正确识别客观阻塞并停止"}
    assert na_dimensions == ["操作完成度"]


def test_question_rubrics_keep_empty_optional_operation_fields() -> None:
    config_dir = Path(__file__).resolve().parents[1] / "config"
    default = load_config(config_dir).domain_skills["default"]

    assert default.operation_policy is None
    assert default.rubrics
    assert all(dim.criteria == [] for dim in default.rubrics)
    assert all(dim.score_anchors == {} for dim in default.rubrics)
