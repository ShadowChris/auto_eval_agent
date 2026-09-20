"""任务类数据集修订的轻量状态与统计辅助函数。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


ACTIVE = "active"
EXCLUDED = "excluded"


def is_item_active(item: dict[str, Any]) -> bool:
    """旧快照没有状态字段时按有效数据处理。"""
    return str(item.get("dataset_status") or ACTIVE) != EXCLUDED


def active_item_indices(items: list[dict[str, Any]]) -> list[int]:
    return [index for index, item in enumerate(items) if is_item_active(item)]


def result_index(result: dict[str, Any]) -> int | None:
    try:
        return int(result.get("index"))
    except (TypeError, ValueError):
        return None


def active_results(
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    active = set(active_item_indices(items))
    return [row for row in results if result_index(row) in active]


def active_result_count(
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> int:
    return len({
        index for row in active_results(items, results)
        if (index := result_index(row)) is not None
    })


def active_success_count(
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> int:
    return len({
        index for row in active_results(items, results)
        if "error" not in row and (index := result_index(row)) is not None
    })


def batch_run_state(
    status: str | None,
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, int | str]:
    """将内部执行状态转换为稳定的对外批跑状态。

    ``progress`` 只统计成功生成评估结果的题目；``processed`` 同时包含
    成功和逐题失败结果。这样部分完成与整批失败可以由 ``progress/total``
    明确定义，同时不丢失已经执行过的题目数量。
    """
    raw_status = str(status or "").strip().lower()
    total = active_total(items)
    progress = min(active_success_count(items, results), total)
    processed = min(active_result_count(items, results), total)

    if raw_status in {"pending", "running", "rerunning"}:
        public_status = "running"
    elif raw_status in {"cancelled", "canceled"}:
        public_status = "cancelled"
    elif total == 0:
        public_status = "failed" if raw_status in {"error", "failed"} else "completed"
    elif progress >= total:
        public_status = "completed"
    elif progress > 0:
        public_status = "partial_completed"
    else:
        public_status = "failed"

    return {
        "status": public_status,
        "progress": progress,
        "processed": processed,
        "total": total,
    }


def tracked_item(
    item: dict[str, Any],
    *,
    batch_id: str,
    revision: int = 1,
    status: str = ACTIVE,
) -> dict[str, Any]:
    """复制输入行并附加不进入模型 Prompt 的数据集修订元数据。"""
    row = deepcopy(item)
    row["dataset_status"] = status
    row["dataset_revision"] = max(1, int(revision))
    row["dataset_source_batch_id"] = batch_id
    return row


def active_total(items: list[dict[str, Any]]) -> int:
    return sum(is_item_active(item) for item in items)
