"""任务类（录屏）裁判输出字段的结构归一化。"""
from __future__ import annotations


def normalize_operation_fields(
    correctness: str,
    error_type,
    is_low_level,
    task_type: str | None = None,
) -> tuple[str | None, str]:
    """保证非 right 有错因，并把“是否低级”规范为 yes/no。"""
    if correctness == "right":
        return None, "no"

    normalized_error = str(error_type).strip() if error_type is not None else ""
    if not normalized_error or normalized_error.lower() in {"null", "none", "n/a"}:
        normalized_error = "未归因"

    if correctness == "unclear":
        return normalized_error, "no"
    if str(task_type or "").strip().lower() == "complex":
        return normalized_error, "no"

    if isinstance(is_low_level, bool):
        normalized_low_level = "yes" if is_low_level else "no"
    else:
        raw = str(is_low_level or "").strip().lower()
        normalized_low_level = "yes" if raw in {"yes", "true", "1", "是"} else "no"
    return normalized_error, normalized_low_level
