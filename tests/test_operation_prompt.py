from pathlib import Path

from auto_eval.config import load_config
from auto_eval.judges.operation_fields import normalize_operation_fields
from auto_eval.judges.rubric_judge import _flatten_rubric
from auto_eval.judges.prompts import OPERATION_SYSTEM, OPERATION_USER


def _operation_prompt() -> tuple[object, str]:
    config_dir = Path(__file__).resolve().parents[1] / "config"
    operation = load_config(config_dir).domain_skills["operation"]
    prompt = OPERATION_SYSTEM.render(
        persona="测试裁判",
        agent_claim="任务已经完成",
        dims=operation.rubrics,
        scale=5,
    )
    return operation, prompt


def test_operation_uses_two_whole_query_dimensions() -> None:
    operation, prompt = _operation_prompt()

    assert [dim.name for dim in operation.rubrics] == ["操作完成度", "步骤正确性"]
    assert [dim.weight for dim in operation.rubrics] == [0.7, 0.3]
    assert operation.rubrics[0].criteria
    assert operation.rubrics[0].score_anchors[5].startswith("整个 query 完整闭环")
    assert operation.rubrics[1].score_anchors[1].startswith("路径完全错误")
    assert "最终态正确" not in prompt
    assert "效率与稳健性" not in prompt


def test_operation_prompt_renders_dimension_configuration() -> None:
    _, prompt = _operation_prompt()

    assert "1. 操作完成度（权重 0.7，1–5 分）" in prompt
    assert "定义：基于整个 query 判断用户目标是否完整闭环。" in prompt
    assert "- 最终状态是否满足整个 query" in prompt
    assert "- 5分：整个 query 完整闭环" in prompt
    assert "2. 步骤正确性（权重 0.3，1–5 分）" in prompt
    assert "- 是否发生与 query 相关的实际操作" in prompt
    assert "- 1分：路径完全错误" in prompt


def test_operation_prompt_distinguishes_simple_and_complex_tasks() -> None:
    _, prompt = _operation_prompt()

    assert "简单任务" in prompt
    assert "复杂多任务" in prompt
    assert "简单单任务" not in prompt
    assert "不要把 query 拆成多个独立评测 case" in prompt
    assert "复杂多任务中任意一项 query 明确要求的结果未完成" in prompt
    assert "其他任务已经成功" in prompt
    assert "任一目标未完成即 wrong" in prompt


def test_operation_prompt_defines_strict_partial_and_special_cases() -> None:
    _, prompt = _operation_prompt()

    assert "结果完成但过程有瑕疵" in prompt
    assert "partial 的前提是整个 query 的所有结果都已完成" in prompt
    assert "简单任务的最终动作未执行" in prompt
    assert "即使只差最后一次保存、确认、开启、拍摄或发送" in prompt
    assert "操作前已经满足且有画面证据，也判 right" in prompt
    assert "仅有文字声明、操作卡片或口头指导而无第1类最终状态证据" in prompt
    assert "仅文字无状态证据" in prompt
    assert "待权限授权" in prompt
    assert "录屏证据缺失" in prompt
    assert '"操作完成度": {"total": <1-5 整数>, "reason":' in prompt
    assert '"步骤正确性": {"total": <1-5 整数>, "reason":' in prompt


def test_operation_prompt_limits_the_current_task_window() -> None:
    _, prompt = _operation_prompt()

    assert "【当前任务的有效时间窗】" in prompt
    assert "当前 query 之前出现的历史聊天、旧任务和旧操作不是本题步骤" in prompt
    assert "当前任务已经明确完成后，用户发生的复制、滚动、返回或开始新任务等行为" in prompt
    assert "不得因此把 right 降为 partial" in prompt


def test_operation_prompt_requires_independent_completion_evidence() -> None:
    _, prompt = _operation_prompt()

    assert "【证据层级与使用规则】" in prompt
    assert "文字声明：agent 回复“已完成/已打开/已设置”等，只是自述" in prompt
    assert "right 必须有第1类证据" in prompt
    assert "“正在操作/已结束操作”本身不能证明最终目标已经达成" in prompt
    assert "只有第2类或第3类证据时不能判 right" in prompt
    assert "状态栏在下午直接显示“17:04”可以证明当前已是 24 小时制" in prompt
    assert "“声音模式设置成静音了”这种聊天回复本身不是静音状态证据" in prompt


def test_operation_prompt_has_pre_output_correctness_gates() -> None:
    _, prompt = _operation_prompt()

    assert "【输出前硬校验】" in prompt
    assert "写不出则禁止输出 right" in prompt
    assert "任一目标未完成、只差最后一步、只有计划/尝试/文字声明时" in prompt
    assert "若失败确由输入缺失、必要授权、账号、硬件、设备能力或样本损坏导致" in prompt
    assert "不得仅因为缺少状态变化前后对比而降为 unclear" in prompt


def test_operation_prompt_prioritizes_blockers_and_fact_verification() -> None:
    _, prompt = _operation_prompt()

    assert "设备、账号、硬件或应用客观不具备完成条件" in prompt
    assert "必须等待用户授权、登录、选择、确认" in prompt
    assert "unclear 不是普通未完成的兜底" in prompt
    assert "出现“WebSearch/搜索完成”不等于事实正确" in prompt
    assert "事实结果无法核验" in prompt


def test_operation_prompt_ignores_recording_infrastructure() -> None:
    operation, prompt = _operation_prompt()

    assert "【录屏载体噪声】" in prompt
    assert "默认是制作评测录屏的基础设施，不是 agent 在当前 query 中执行的操作" in prompt
    assert "不得据此认定 agent 开启了屏幕录制" in prompt
    assert "顶部黑色胶囊中的评测录屏计时持续增长，也绝不能据此判 right" in prompt
    assert "顶部状态栏或灵动岛黑色胶囊中的红点和递增计时永远不能证明相机正在录像" in prompt
    assert "相机处于“录像”模式但仍显示可点击的红色圆形开始按钮时" in prompt
    assert "若 query 是相机录像且准备输出 right" in prompt
    assert any("录屏工具自身的计时" in criterion for criterion in operation.rubrics[1].criteria)


def test_operation_prompt_marks_missing_final_action_wrong() -> None:
    operation, prompt = _operation_prompt()

    assert "最终动作尚未执行，应判 wrong" in prompt
    assert "停在可直接执行去水印/保存的最后一步，判 wrong" in prompt
    assert "已进入相机录像模式，仍显示可点击的红色圆形开始按钮" in prompt
    assert operation.rubrics[0].score_anchors[3].startswith("已完成主要过程")


def test_operation_user_prompt_repeats_recording_noise_warning() -> None:
    user_prompt = OPERATION_USER.render(question="给我录像", context="")

    assert "重要录屏提示" in user_prompt
    assert "默认不是 agent 的操作，也不是相机正在录像的证据" in user_prompt
    assert "相机录像状态必须从相机应用内部控件判断" in user_prompt


def test_operation_prompt_prioritizes_pending_user_input_as_unclear() -> None:
    _, prompt = _operation_prompt()

    assert "必须等待用户授权、登录、选择、确认" in prompt
    assert "未找到该笔记，是否新建后记录" in prompt
    assert "只打开图库并等待用户选择具体照片后才能去水印" in prompt
    assert "query 本身缺少必要信息" in prompt
    assert "最终因用户未确认而超时终止" in prompt
    assert "用户未授权或未确认导致超时必须判 unclear" in prompt
    assert "最终状态证据略弱" not in prompt


def test_operation_prompt_requires_error_type_and_low_level_flag() -> None:
    _, prompt = _operation_prompt()

    assert "只要 correctness 不是 right，error_type 必须输出一个非空标签" in prompt
    assert "【是否低级 is_low_level】" in prompt
    assert "right 和 unclear 固定输出 no" in prompt
    assert "如果任务形态是复杂多任务，is_low_level 固定输出 no" in prompt
    assert "不要因为结果是 wrong 就自动输出 yes" in prompt
    assert '"task_type": "simple|complex"' in prompt
    assert '"is_low_level": "yes|no"' in prompt


def test_operation_output_fields_are_normalized() -> None:
    assert normalize_operation_fields("right", "路径错误", "yes") == (None, "no")
    assert normalize_operation_fields("wrong", None, True) == ("未归因", "yes")
    assert normalize_operation_fields("partial", "路径冗余", "是") == ("路径冗余", "yes")
    assert normalize_operation_fields("unclear", "待权限授权", "yes") == ("待权限授权", "no")
    assert normalize_operation_fields(
        "wrong",
        "仅文字无状态证据",
        "yes",
        "complex",
    ) == ("仅文字无状态证据", "no")


def test_operation_total_reason_output_is_flattened_with_reasons() -> None:
    rubric, reasons, na_dimensions = _flatten_rubric({
        "操作完成度": {"total": 3, "reason": "还有一项任务未完成"},
        "步骤正确性": {"total": 4, "reason": "路径正确但有一次重复点击"},
    }, dim_names=["操作完成度", "步骤正确性"])

    assert rubric == {"操作完成度": 3, "步骤正确性": 4}
    assert reasons == {
        "操作完成度": "还有一项任务未完成",
        "步骤正确性": "路径正确但有一次重复点击",
    }
    assert na_dimensions == []


def test_question_rubrics_keep_empty_optional_operation_fields() -> None:
    config_dir = Path(__file__).resolve().parents[1] / "config"
    default = load_config(config_dir).domain_skills["default"]

    assert default.rubrics
    assert all(dim.criteria == [] for dim in default.rubrics)
    assert all(dim.score_anchors == {} for dim in default.rubrics)
