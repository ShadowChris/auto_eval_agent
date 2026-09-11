"""dev_yang 历史快照 → 报告/对比 payload 的适配层。

把垂域视觉评测快照（problem_solved / answer_issues 口径）投影成报告
组件统一消费的批次行（correctness / issue_types 口径），统计与匹配
计算全部复用 analysis 层纯函数，Web 汇总与 HTML 报告共用一套口径。
"""
from __future__ import annotations

import re
from typing import Any

from ..analysis.operation_comparison import compare_operation_batches
from ..analysis.operation_report import (
    CASE_DETAIL_FIELDS,
    build_comparison_report,
    build_single_report,
)
from ..analysis.operation_statistics import summarize_operation_results


REPORT_MODE = "rich_content"


def _issue_labels(value: Any) -> list[str]:
    """answer_issues 每行形如「标签：具体描述」，取标签并同题去重保序。"""
    if not isinstance(value, str):
        return []
    labels: list[str] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        colon = re.search(r"[：:]", line)
        label = (line[: colon.start()] if colon and colon.start() > 0 else line).strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _result_projection(result: Any) -> dict[str, Any]:
    """单条结果行 → 报告口径的紧凑 result；失败行只保留 error。"""
    if not isinstance(result, dict) or not result:
        return {}
    if result.get("error"):
        return {"error": str(result.get("error") or "")}
    projected: dict[str, Any] = {
        "correctness": str(result.get("problem_solved") or ""),
        "issue_types": _issue_labels(result.get("answer_issues")),
    }
    for field in CASE_DETAIL_FIELDS:
        if field in projected:
            continue
        value = result.get(field, "")
        if field == "needs_review":
            # bool → 稀疏 yes/空，报告详情里 false 不显示
            value = "yes" if value else ""
        projected[field] = value
    return projected


def _snapshot_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """items 按位置对齐 results 的 index；行 index 用题号（无则用位置）。"""
    items = snapshot.get("items") or []
    results_by_index: dict[int, dict[str, Any]] = {}
    for row in snapshot.get("results") or []:
        index = row.get("index")
        if isinstance(index, int):
            results_by_index[index] = row  # legacy 同 index 重复时取最新
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        item = item or {}
        result = results_by_index.pop(position, None)
        item_id = str(
            (result or {}).get("item_id") or item.get("id") or f"q{position}"
        )
        query = str(
            (result or {}).get("query") or item.get("query") or item.get("question") or ""
        )
        rows.append({
            "index": item_id,
            "item_id": item_id,
            "query": query,
            "result": _result_projection(result),
        })
    # 结果 index 超出 items 范围的 legacy 行兜底保留
    for position, result in sorted(results_by_index.items()):
        rows.append({
            "index": str(result.get("item_id") or f"q{position}"),
            "item_id": str(result.get("item_id") or f"q{position}"),
            "query": str(result.get("query") or ""),
            "result": _result_projection(result),
        })
    return rows


def operation_statistics_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """构建供 API、Web 和 HTML 报告共用的统计 JSON。"""
    if snapshot.get("mode") != REPORT_MODE:
        raise ValueError("仅垂域视觉评测支持统计分布")
    rows = _snapshot_rows(snapshot)
    return {
        "schema_version": 1,
        "task_id": snapshot.get("task_id") or "",
        "dataset_name": snapshot.get("dataset_name") or "",
        "mode": REPORT_MODE,
        "statistics": summarize_operation_results(
            [row["result"] for row in rows],
            total_cases=len(rows),
        ),
    }


def operation_comparison_batch(snapshot: dict[str, Any]) -> dict[str, Any]:
    """将垂域视觉评测历史快照转换为批次对比所需的紧凑输入。"""
    if snapshot.get("mode") != REPORT_MODE:
        raise ValueError("仅垂域视觉评测历史支持批次对比")
    return {
        "task_id": str(snapshot.get("task_id") or ""),
        "dataset_name": str(snapshot.get("dataset_name") or ""),
        "created_at": snapshot.get("created_at"),
        "rows": _snapshot_rows(snapshot),
    }


def single_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """单批统计图表报告 payload（Web 报告组件与离线 HTML 共用）。"""
    batch = operation_comparison_batch(snapshot)
    statistics = operation_statistics_payload(snapshot)["statistics"]
    return build_single_report(batch, statistics)


def comparison_report(
    snapshots: list[dict[str, Any]],
    *,
    baseline_task_id: str,
) -> dict[str, Any]:
    """多批对比 payload，附图表报告所需的配对 Case 引用。"""
    batches = [operation_comparison_batch(snapshot) for snapshot in snapshots]
    comparison = compare_operation_batches(
        batches,
        baseline_task_id=baseline_task_id,
    )
    comparison["report"] = build_comparison_report(batches, comparison)
    return comparison
