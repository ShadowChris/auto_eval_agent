from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_eval.config import load_config
from auto_eval.judges.operation_fields import (
    map_legacy_operation_result,
    normalize_operation_fields,
)
from auto_eval.judges.prompts import ARBITRATOR_SYSTEM, OPERATION_SYSTEM, OPERATION_USER
from auto_eval.judges.rubric_judge import _flatten_rubric
from auto_eval.schema import OperationSingleScore


def _operation_prompt():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    operation = load_config(config_dir).domain_skills["operation"]
    prompt = OPERATION_SYSTEM.render(
        persona="测试裁判",
        dims=operation.rubrics,
        scale=5,
        policy=operation.operation_policy,
    )
    return operation, prompt


def test_operation_policy_and_dimensions_load_from_yaml() -> None:
    operation, prompt = _operation_prompt()

    assert [dim.name for dim in operation.rubrics] == ["操作完成度", "步骤正确性"]
    assert [dim.weight for dim in operation.rubrics] == [0.7, 0.3]
    assert operation.operation_policy is not None
    assert operation.operation_policy.prior_knowledge
    assert operation.operation_policy.scope_rules
    assert operation.operation_policy.evidence_rules
    assert list(operation.operation_policy.correctness) == [
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
    assert "不符合 ok、nok、no_support 任一条件的其他类别" in prompt
    assert "如果已经能够确认任务完成、可归责执行失败或客观条件阻塞" in prompt
    assert "未预期场景" in prompt
    assert "其他未归类情况" in prompt
    assert "任一生效目标未完成" in prompt
    assert '"correctness": "ok|nok|no_support|others"' in prompt
    assert '"correctness": "right|wrong|partial|unclear"' not in prompt


def test_operation_prompt_treats_only_severe_response_quality_as_nok() -> None:
    operation, prompt = _operation_prompt()

    assert "与 query 严重不相关" in prompt
    assert "大段机械重复影响阅读" in prompt
    assert "包含大量无关或乱码等冗余字符" in prompt
    assert "暴露无必要的检索过程、skill/工具名称、内部推理及思维链" in prompt
    assert "严重回复质量问题" in prompt
    assert "内部过程信息泄露" in prompt
    assert "不影响理解和任务闭环的少量重复" in prompt
    assert "最终文字回复是否与 query 相关、可读且能清晰传达结果" in prompt
    assert "最终文字回复是否泄露无必要的检索、skill、工具调用、内部推理或思维链" in prompt
    assert "操作完成但最终回复存在严重质量问题或内部过程信息泄露" in prompt
    assert "严重回复质量问题" in operation.operation_policy.issue_types["nok"]
    assert "内部过程信息泄露" in operation.operation_policy.issue_types["nok"]


def test_operation_prompt_handles_conditional_tasks_and_causality() -> None:
    _, prompt = _operation_prompt()

    assert "只要求完成条件实际成立后所激活的目标" in prompt
    assert "条件不成立时正确跳过后续动作，也属于完成" in prompt
    assert "条件分支错误" in prompt
    assert "条件判断错误" in prompt
    assert "将每个生效目标判断为已完成、客观阻塞、评测侧无法判断或可归责未完成" in prompt
    assert "任一生效目标存在可归责未完成、执行错误或严重回复质量问题时判 nok" in prompt
    assert "未完成目标全部由外部条件阻塞则判 no_support" in prompt


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
    assert "最终画面或初始状态直接满足 query 即可" in prompt
    assert "关键帧未展示全部过渡过程不等于步骤错误" in prompt
    assert "逐个生效目标说明最终状态、对应证据或阻塞" in prompt
    assert "事实无法核验时判 others" in prompt
    assert "自动操作卡片、进度、目标入口和操作轨迹属于过程证据" not in prompt
    assert "ok 必须有最终状态强证据" not in prompt


def test_operation_prompt_separates_collapsed_window_from_plain_text_claim() -> None:
    _, prompt = _operation_prompt()

    assert "已结束操控，点击查看" in prompt
    assert "任务执行窗口始终处于带“查看/点击查看”入口的缩略状态" in prompt
    assert "判 others，优先标记任务执行窗口未展开" in prompt
    assert "相关评分维度填 null" in prompt
    assert "录屏数据完整时判 nok" in prompt
    assert "标记仅文字声称完成或完成证据不足" in prompt
    assert "任务执行窗口未展开" in prompt


def test_operation_prompt_ignores_recording_infrastructure() -> None:
    operation, prompt = _operation_prompt()

    assert "【评测先验知识】" in prompt
    assert "来自评测录屏工具，不是 agent 操作" in prompt
    assert "顶部状态栏或灵动岛的红点和计时不能证明相机正在录像" in prompt
    assert "相机应用内部的停止或暂停按钮" in prompt
    assert not any(
        "录屏工具自身的计时" in criterion
        for criterion in operation.rubrics[1].criteria
    )
    assert "【录屏载体噪声】" not in prompt


def test_operation_prompt_requires_issue_types_and_low_level_flag() -> None:
    _, prompt = _operation_prompt()

    assert "【issue_types】" in prompt
    assert "输出中文字符串数组" in prompt
    assert "nok、no_support、others 至少填写一项" in prompt
    assert "只有意图清晰的简单任务被判 nok" in prompt
    assert "复杂多任务固定输出 no" in prompt
    assert '"issue_types": ["<受控中文问题类型>"]' in prompt
    assert '"is_low_level": "yes|no"' in prompt
    assert "error_type" not in prompt


def test_operation_arbitrator_reuses_the_same_policy() -> None:
    operation, _ = _operation_prompt()
    prompt = ARBITRATOR_SYSTEM.render(
        operation_mode=True,
        dims=operation.rubrics,
        policy=operation.operation_policy,
    )

    assert "- ok：" in prompt
    assert "当前任务类录屏使用的评测手机未安装 SIM 卡" in prompt
    assert "完成证据不足" in prompt
    assert "待权限授权" in prompt
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
    ) == ("nok", ["最终步骤未执行"], "yes")
    assert normalize_operation_fields(
        "nok", None, True, "simple", allowed
    ) == ("nok", ["其他执行问题"], "yes")
    assert normalize_operation_fields(
        "no_support", ["待权限授权"], "yes", "simple", allowed
    ) == ("no_support", ["待权限授权"], "no")
    assert normalize_operation_fields(
        "others", "视频损坏；自定义标签", "yes", "simple", allowed
    ) == ("others", ["视频损坏", "其他未归类情况"], "no")
    assert normalize_operation_fields(
        "nok", ["尚未定义的执行错误"], "no", "simple", allowed
    ) == ("nok", ["其他执行问题"], "no")
    assert normalize_operation_fields(
        "nok", ["完成证据不足"], "yes", "complex", allowed
    ) == ("nok", ["完成证据不足"], "no")
    assert normalize_operation_fields(
        "ok", ["严重回复质量问题"], "yes", "simple", allowed
    ) == ("nok", ["严重回复质量问题"], "yes")
    assert normalize_operation_fields(
        "ok", ["内部过程信息泄露"], "yes", "simple", allowed
    ) == ("nok", ["内部过程信息泄露"], "yes")


def test_legacy_operation_results_map_at_read_time() -> None:
    assert map_legacy_operation_result("right", None) == ("ok", [])
    assert map_legacy_operation_result("partial", "路径冗余") == (
        "ok",
        ["路径冗余"],
    )
    assert map_legacy_operation_result("wrong", "仅文字无状态证据") == (
        "nok",
        ["仅文字声称完成"],
    )
    assert map_legacy_operation_result("unclear", "待权限授权") == (
        "no_support",
        ["待权限授权"],
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
