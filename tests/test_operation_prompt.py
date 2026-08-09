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
    assert config.expert_knowledge["operation"].version == 2
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
    assert "不得再为同一中断所阻塞的目标重复标记" in prompt
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
    assert "模型收到的画面没有可验证结果时判 nok" in prompt
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


def test_operation_prompt_distinguishes_guided_user_wait_from_silent_stall() -> None:
    _, prompt = _operation_prompt()

    assert "对未完成目标必须识别其最后有效状态" in prompt
    assert "界面、任务执行过程文字或 agent 最终回答明确提示用户登录、授权、验证" in prompt
    assert "普通登录入口、设置页、应用首页、加载状态、任务超时或终止本身" in prompt
    assert "没有明确指引、最终回答为空且流程停止或超时" in prompt
    assert "超时或终止只是结果，不能单独证明等待用户" in prompt
    assert "缺少用户后续操作和最终结果只是阻塞的必然后果" in prompt
    assert "已经明确等待用户的目标，不得再因用户未响应" in prompt
    assert "不应要求 agent 代替用户点击同意或输入凭据" in prompt
    assert "不评价是否存在其他更优的免询问策略" in prompt
    assert "agent 明确说明助手、设备或系统不支持" in prompt
    assert "没有可信专家经验或可见证据反驳" in prompt
    assert "必须同时检查按时间顺序的视觉证据和 agent 文本自述" in prompt
    assert "不得只依据其中一路下结论" in prompt
    assert "任务执行过程文字或 agent 最终回答" in prompt
    assert "返回桌面或返回助手界面" in prompt
    assert "缺少完成任务所需的信息并指引用户提供" in prompt
    assert "泛化能力常识、相似能力或裁判设想的替代策略" in prompt
    assert "必要性、因果性和时效性" in prompt
    assert "先走错或执行失败后询问用户换路径" in prompt
    assert "早先指引已经完成、关闭或越过" in prompt
    assert "通常可以通过其他网络、应用、入口或方法完成" in prompt
    assert "不强制要求 agent 再重复说明" in prompt


def test_operation_prompt_does_not_expand_goal_or_treat_recovery_question_as_blocker() -> None:
    _, prompt = _operation_prompt()

    assert "不得擅自扩大 query 的完成边界" in prompt
    assert "可以按打开、查看或展示该功能入口理解" in prompt
    assert "query 已提供继续执行所需信息" in prompt
    assert "只是错误后的恢复询问" in prompt
    assert "不能把此前错误改写为等待用户" in prompt


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
    assert "已经展示一个航班、酒店或商品候选项" in prompt
    assert "合理搜索未找到 query 指定对象后" in prompt
    assert "不相关关键词" in prompt
    assert "按 no_support / 待用户澄清处理" in prompt


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
    assert "阻塞发生前是否已有与用户未响应无关" in prompt
    assert "禁止把任务面板的步骤、“已完成”或“超时，任务终止”当成 agent 回复" in prompt
    assert "必须引用冲突的具体 agent 自然语言回复" in prompt


def test_operation_prompt_requires_blocker_evidence_gate_before_correctness() -> None:
    _, prompt = _operation_prompt()

    assert "必须先完成“阻塞证据门控”" in prompt
    assert "逐帧扫描并逐字摘录面向用户的登录、授权、同意" in prompt
    assert "协议说明配合“取消/同意”等按钮" in prompt
    assert "从完整 agent 文本中逐字摘录“无法/缺少/未提供/请提供" in prompt
    assert "不得在未逐条处理已看见指引文字的情况下声称“没有引导”" in prompt
    assert "阻塞证据门控的映射是强约束" in prompt
    assert "【视觉指引扫描】" in prompt
    assert "【文本指引扫描】" in prompt
    assert "【独立错误检查】" in prompt
    assert "【强制映射】" in prompt
    assert "只有能够逐字引用的、直接要求或询问用户采取下一步" in prompt
    assert "普通“登录”入口、“查看”按钮、“手动操作中”状态" in prompt
    assert "不得根据图标、模式名称、页面类型或预期交互推测" in prompt
    assert "不得意译或补写原文中没有的询问" in prompt


def test_operation_prompt_scopes_blocker_and_requires_direct_playback_evidence() -> None:
    _, prompt = _operation_prompt()

    assert "用户阻塞只作用于确实依赖该次用户答复的目标" in prompt
    assert "不能自动豁免其他相互独立的目标" in prompt
    assert "不得仅因任务采用串行执行" in prompt
    assert "搜索结果卡、影视详情卡、播放按钮" in prompt
    assert "不能证明播放动作已经发生" in prompt
    assert "播放器界面、播放进度、暂停按钮" in prompt
    assert "【结果证据专项】" in prompt


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
    ) == ("nok", ["未展示可验证结果"], "no")
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


def test_legacy_operation_results_map_at_read_time() -> None:
    assert map_legacy_operation_result("right", None) == ("ok", [])
    assert map_legacy_operation_result("partial", "路径冗余") == (
        "ok",
        ["路径冗余"],
    )
    assert map_legacy_operation_result("wrong", "仅文字无状态证据") == (
        "nok",
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
