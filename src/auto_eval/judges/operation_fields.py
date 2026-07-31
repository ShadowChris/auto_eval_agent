"""任务类（录屏）裁判输出字段的结构归一化。"""
from __future__ import annotations

import re
from typing import Any

from ..schema import OperationCorrectness

_VALID_CORRECTNESS = {"ok", "nok", "no_support", "others"}
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
    "路径错误": "操作路径错误",
    "单任务未完成": "任务未完成",
    "仅文字无状态证据": "仅文字声称完成",
    "自述与界面冲突": "界面与回复不一致",
    "待账号登陆": "待账号登录",
    "不具备完成条件": "设备或系统不支持",
    "录屏证据缺失": "录屏数据不完整",
    "证据冲突": "评测证据冲突",
    "未归因": "其他执行问题",
    "其他不可评估原因": "其他未归类情况",
}
_FALLBACK_ISSUE = {
    "ok": "其他轻微问题",
    "nok": "其他执行问题",
    "no_support": "其他阻塞原因",
    "others": "其他未归类情况",
}


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


def normalize_operation_fields(
    correctness: Any,
    issue_types: Any,
    is_low_level: Any,
    task_type: str | None = None,
    allowed_issue_types: dict[str, list[str]] | None = None,
) -> tuple[OperationCorrectness, list[str], str]:
    """规范 correctness、中文问题数组和低级错误标识。"""
    normalized_correctness = normalize_operation_correctness(correctness, issue_types)
    issues = _as_issue_list(issue_types)

    if allowed_issue_types:
        allowed_by_status = {
            key: list(dict.fromkeys(values))
            for key, values in allowed_issue_types.items()
        }
        all_allowed = {
            issue
            for values in allowed_by_status.values()
            for issue in values
        }
        issue_status = {
            issue: status
            for status, values in allowed_by_status.items()
            for issue in values
        }
        if normalized_correctness == "ok":
            conflicting_status = next(
                (
                    issue_status[issue]
                    for issue in issues
                    if issue_status.get(issue) in {"nok", "no_support", "others"}
                ),
                None,
            )
            if conflicting_status is not None:
                normalized_correctness = conflicting_status  # type: ignore[assignment]
        unknown = any(issue not in all_allowed for issue in issues)
        issues = [issue for issue in issues if issue in all_allowed]
        primary_allowed = set(allowed_by_status.get(normalized_correctness, []))
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
