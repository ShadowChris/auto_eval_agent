"""垂域视觉评测图表报告的数据投影；复用现有统计和匹配口径，不携带媒体或模型轨迹。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .operation_comparison import _is_valid, _match_rows
from .operation_statistics import normalize_operation_issue_types


# 报告 Case 白名单字段：显式投影，避免 HTML 携带 base64 帧、原始调用或
# 内部调试信息。字段名即 dev_yang 结果行的字段名。
CASE_DETAIL_FIELDS = (
    "context",
    "category_display",
    "answer_text",
    "problem_solved_reason",
    "answer_issues",
    "needs_review",
    "review_reason",
    "factual_conflict",
    "factual_conflict_detail",
    "card_count",
    "superlink_count",
    "rationale",
    "latency_s",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def report_case(row: dict[str, Any]) -> dict[str, Any]:
    """显式字段投影，避免 HTML 携带帧数据或原始调用日志。"""
    result = row.get("result") or {}
    case = {
        "index": row.get("index", ""),
        "item_id": row.get("item_id", ""),
        "query": row.get("query", ""),
        "valid": _is_valid(result),
        "correctness": result.get("correctness", ""),
        "issue_types": list(dict.fromkeys(
            normalize_operation_issue_types(result.get("issue_types"))
        )),
    }
    for field in CASE_DETAIL_FIELDS:
        case[field] = result.get(field, "")
    return _json_value(case)


def build_single_report(batch: dict[str, Any], statistics: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "single",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_name": batch.get("dataset_name") or "",
        "task_id": batch.get("task_id") or "",
        "statistics": statistics,
        "cases": [
            report_case(row) for row in batch.get("rows") or []
            if _is_valid(row.get("result") or {})
        ],
    }


def build_comparison_report(
    batches: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    """逐组仅保留一份 Case 池；配对用索引引用，避免多实验组复制整份对照数据。"""
    by_id = {batch["task_id"]: batch for batch in batches}
    baseline = by_id[comparison["baseline_task_id"]]
    baseline_rows = baseline.get("rows") or []
    groups = [
        {
            **group,
            "cases": [report_case(row) for row in by_id[group["task_id"]].get("rows") or []],
        }
        for group in comparison["groups"]
    ]
    pair_fields = (
        "baseline_task_id", "target_task_id", "baseline_label", "target_label",
        "matched_count", "valid_pair_count", "baseline_ok_count", "baseline_ok_rate",
        "target_ok_count", "target_ok_rate", "ok_rate_delta", "ok_rate_change",
        "ok_rate_change_label", "to_ok_count", "from_ok_count", "net_ok_change",
        "issue_type_rows", "conclusion",
    )
    pairs = []
    for pair in comparison["pairwise"]:
        target_rows = by_id[pair["target_task_id"]].get("rows") or []
        matches, _ = _match_rows(baseline_rows, target_rows)
        valid_matches = [
            [match["baseline_index"], match["target_index"]]
            for match in matches
            if _is_valid(baseline_rows[match["baseline_index"]].get("result") or {})
            and _is_valid(target_rows[match["target_index"]].get("result") or {})
        ]
        pairs.append({
            **{field: pair.get(field) for field in pair_fields},
            "matches": valid_matches,
        })
    return {
        "schema_version": 1,
        "kind": "comparison",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_task_id": comparison["baseline_task_id"],
        "all_groups_common_valid_count": comparison["all_groups_common_valid_count"],
        "ok_rate_close_threshold": comparison["ok_rate_close_threshold"],
        "groups": groups,
        "pairs": pairs,
    }
