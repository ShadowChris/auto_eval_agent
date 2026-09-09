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
    return sum("error" not in row for row in active_results(items, results))


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
