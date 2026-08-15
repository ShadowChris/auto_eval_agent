"""任务类（录屏）裁判输出字段的结构归一化。"""
from __future__ import annotations

import re
from typing import Any

from ..schema import OperationCorrectness

_VALID_CORRECTNESS = {"ok", "nok", "no_support", "others"}
QUERY_ALIGNMENT_ISSUE = "录屏Query无法与输入Query一致核验"
_LEGACY_CORRECTNESS = {
    "right": "ok",
    "partial": "ok",
    "wrong": "nok",
}
_NO_SUPPORT_HINTS = (
    "权限",
    "授权",
    "登录",
    "信息补充",
    "待用户澄清",
    "缺少前置条件",
    "身份验证",
    "敏感操作",
    "应用未安装",
    "目标对象不存在",
    "不具备完成条件",
    "设备",
    "硬件",
    "外部服务",
)
_ALIASES = {
    "路径错误": "任务结果错误",
    "单任务未完成": "应执行目标未执行",
    "任务未完成": "应执行目标未执行",
    "多任务未全部完成": "应执行目标未执行",
    "最终步骤未执行": "应执行目标未执行",
    "仅文字无状态证据": "未展示可验证结果",
    "仅文字声称完成": "未展示可验证结果",
    "完成证据不足": "未展示可验证结果",
    "最终状态错误": "任务结果错误",
    "操作对象错误": "任务结果错误",
    "操作参数错误": "任务结果错误",
    "操作路径错误": "任务结果错误",
    "条件判断错误": "任务结果错误",
    "条件分支错误": "任务结果错误",
    "严重意图偏离": "任务结果错误",
    "产生不当副作用": "任务结果错误",
    "自述与界面冲突": "回复与界面不一致",
    "界面与回复不一致": "回复与界面不一致",
    "严重回复质量问题": "其他执行问题",
    "最终回复瑕疵": "其他轻微问题",
    "待账号登陆": "缺少前置条件",
    "待账号登录": "缺少前置条件",
    "待权限授权": "缺少前置条件",
    "待信息补充": "待用户澄清",
    "待身份验证": "缺少前置条件",
    "待用户敏感操作": "缺少前置条件",
    "应用未安装": "缺少前置条件",
    "目标对象不存在": "缺少前置条件",
    "必要硬件缺失": "缺少前置条件",
    "设备或系统不支持": "缺少前置条件",
    "外部服务不可用": "缺少前置条件",
    "不具备完成条件": "缺少前置条件",
    "录屏证据缺失": "录屏数据不完整",
    "无验证结果": "未展示可验证结果",
    "证据冲突": "评测证据冲突",
    "录屏Query与输入Query不一致": QUERY_ALIGNMENT_ISSUE,
    "录屏Query未完整展示": QUERY_ALIGNMENT_ISSUE,
    "录屏未展示Query": QUERY_ALIGNMENT_ISSUE,
    "未归因": "其他执行问题",
    "其他不可评估原因": "其他未归类情况",
}
_FALLBACK_ISSUE = {
    "ok": "其他轻微问题",
    "nok": "其他执行问题",
    "no_support": "其他阻塞原因",
    "others": "其他未归类情况",
}

_MISNESTED_TOP_LEVEL_FIELDS = (
    "task_type",
    "total",
    "correctness",
    "issue_types",
    "error_type",
    "is_low_level",
    "rationale",
    "confidence",
)


def hoist_misnested_operation_fields(data: dict[str, Any]) -> dict[str, Any]:
    """容错模型把顶层任务类字段误放进 rubric 的常见单层括号错误。"""
    rubric = data.get("rubric")
    if not isinstance(rubric, dict):
        return data
    nested = [name for name in _MISNESTED_TOP_LEVEL_FIELDS if name in rubric]
    if not nested:
        return data
    normalized = dict(data)
    normalized_rubric = dict(rubric)
    for name in nested:
        if name not in normalized:
            normalized[name] = normalized_rubric[name]
        normalized_rubric.pop(name, None)
    normalized["rubric"] = normalized_rubric
    return normalized


def _as_issue_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for raw in values:
        if raw is None:
            continue
        for part in re.split(r"[,，;；、|/]+", str(raw)):
            issue = part.strip()
            if not issue or issue.lower() in {"null", "none", "n/a"}:
                continue
            normalized = _ALIASES.get(issue, issue)
            if normalized not in result:
                result.append(normalized)
    return result


def normalize_operation_correctness(
    correctness: Any,
    issue_types: Any = None,
) -> OperationCorrectness:
    """归一新旧任务类判定；旧 unclear 按原错因区分客观阻塞与不可评估。"""
    raw = str(correctness or "").strip().lower()
    if raw in _VALID_CORRECTNESS:
        return raw  # type: ignore[return-value]
    if raw in _LEGACY_CORRECTNESS:
        return _LEGACY_CORRECTNESS[raw]  # type: ignore[return-value]
    if raw == "unclear":
        text = " ".join(_as_issue_list(issue_types))
        return "no_support" if any(hint in text for hint in _NO_SUPPORT_HINTS) else "others"
    return "others"


def _issue_rule_allowed(rule: Any) -> list[str]:
    if hasattr(rule, "allowed_correctness"):
        return list(rule.allowed_correctness)
    if isinstance(rule, dict):
        values = rule.get("allowed_correctness", [])
        return list(values) if isinstance(values, list) else []
    return []


def operation_issue_catalog(
    issue_type_rules: dict[str, Any] | None,
) -> dict[str, set[str]]:
    """将新目录或旧按 correctness 分组配置统一为 issue -> 允许状态。"""
    if not issue_type_rules:
        return {}
    statuses = {"ok", "nok", "no_support", "others"}
    if set(issue_type_rules) == statuses and all(
        isinstance(values, list) for values in issue_type_rules.values()
    ):
        catalog: dict[str, set[str]] = {}
        for status, values in issue_type_rules.items():
            for issue in values:
                catalog.setdefault(str(issue), set()).add(status)
        return catalog
    return {
        name: set(_issue_rule_allowed(rule))
        for name, rule in issue_type_rules.items()
        if _issue_rule_allowed(rule)
    }


def primary_operation_issue_types(
    correctness: OperationCorrectness,
    issue_type_rules: dict[str, Any] | None,
) -> set[str]:
    """返回可作为整体 correctness 主因的类型；通用质量问题不能作非 ok 主因。"""
    catalog = operation_issue_catalog(issue_type_rules)
    all_statuses = {"ok", "nok", "no_support", "others"}
    if correctness == "ok":
        return {name for name, allowed in catalog.items() if "ok" in allowed}
    if correctness == "nok":
        return {name for name, allowed in catalog.items() if allowed == {"nok"}}
    return {
        name
        for name, allowed in catalog.items()
        if correctness in allowed and allowed != all_statuses
    }


def normalize_operation_fields(
    correctness: Any,
    issue_types: Any,
    is_low_level: Any,
    task_type: str | None = None,
    allowed_issue_types: dict[str, Any] | None = None,
) -> tuple[OperationCorrectness, list[str], str]:
    """规范 correctness、中文问题数组和低级错误标识。"""
    normalized_correctness = normalize_operation_correctness(correctness, issue_types)
    issues = _as_issue_list(issue_types)

    # Query 对齐是评测输入门禁，而非 agent 执行问题。命中后不再混入
    # 其他执行归因，确保下游可以稳定筛出配对异常样本。
    if QUERY_ALIGNMENT_ISSUE in issues:
        return "others", [QUERY_ALIGNMENT_ISSUE], "no"

    # 新标准：仅缺少结果证据属于评测侧无法确认，主判 others；如果同一复杂
    # 任务还存在独立的可归责错误，则保留 nok，并把证据不足作为次要问题。
    if normalized_correctness == "nok" and "未展示可验证结果" in issues:
        if allowed_issue_types:
            issue_catalog = operation_issue_catalog(allowed_issue_types)
            has_independent_nok = any(
                issue_catalog.get(issue) == {"nok"} for issue in issues
            )
        else:
            has_independent_nok = any(
                issue != "未展示可验证结果" for issue in issues
            )
        if not has_independent_nok:
            normalized_correctness = "others"

    if allowed_issue_types:
        catalog = operation_issue_catalog(allowed_issue_types)
        unknown = any(issue not in catalog for issue in issues)
        issues = [
            issue
            for issue in issues
            if normalized_correctness in catalog.get(issue, set())
        ]
        primary_allowed = primary_operation_issue_types(
            normalized_correctness,
            allowed_issue_types,
        )
        primary = next((issue for issue in issues if issue in primary_allowed), None)
        if unknown and _FALLBACK_ISSUE[normalized_correctness] not in issues:
            issues.append(_FALLBACK_ISSUE[normalized_correctness])
        if normalized_correctness != "ok" and primary is None:
            issues.insert(0, _FALLBACK_ISSUE[normalized_correctness])
        elif primary is not None and issues[0] != primary:
            issues.remove(primary)
            issues.insert(0, primary)

    if normalized_correctness != "ok" and not issues:
        issues = [_FALLBACK_ISSUE[normalized_correctness]]

    low_level = "no"
    if normalized_correctness == "nok" and str(task_type or "").strip().lower() != "complex":
        has_blocker = any(
            any(hint in issue for hint in _NO_SUPPORT_HINTS)
            for issue in issues
        )
        if not has_blocker:
            if isinstance(is_low_level, bool):
                low_level = "yes" if is_low_level else "no"
            else:
                raw = str(is_low_level or "").strip().lower()
                low_level = "yes" if raw in {"yes", "true", "1", "是"} else "no"

    return normalized_correctness, list(dict.fromkeys(issues)), low_level


def map_legacy_operation_result(
    correctness: Any,
    error_type: Any,
) -> tuple[OperationCorrectness, list[str]]:
    """读取旧任务结果时转换字段，不修改历史文件。"""
    normalized, issues, _ = normalize_operation_fields(
        correctness,
        error_type,
        "no",
    )
    return normalized, issues
