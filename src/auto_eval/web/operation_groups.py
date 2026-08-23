"""任务类多组评估的数据对齐与执行策略。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any


def normalize_query(value: Any) -> str:
    """只归一空白，不做同义改写，供同 case Query 一致性校验。"""
    return re.sub(r"\s+", " ", str(value or "").strip())


def _case_id(item: dict) -> str:
    source = item.get("source_data") or {}
    value = item.get("case_id") or source.get("case_id")
    return str(value or "").strip()


def align_operation_groups(groups: list[dict]) -> dict:
    """按 case_id 对齐多个已解析的任务类数据集。

    缺少 case_id 的条目保留为单组回退 case；同组重复 case_id 因无法可靠
    配对而标为预校验错误，不静默选择其中一条。
    """
    normalized_groups: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    ordered_case_ids: list[str] = []
    seen_case_ids: set[str] = set()
    standalone: list[tuple[dict, dict, int]] = []

    for group_index, raw_group in enumerate(groups):
        group_id = str(raw_group.get("group_id") or f"group_{group_index + 1}").strip()
        group_name = str(raw_group.get("group_name") or group_id).strip()
        group_role = str(raw_group.get("group_role") or "experiment").strip()
        items = list(raw_group.get("items") or [])
        counts = Counter(_case_id(item) for item in items if _case_id(item))
        duplicates = {key for key, count in counts.items() if count > 1}
        if duplicates:
            errors.append(
                f"{group_name} 存在重复 case_id：{', '.join(sorted(duplicates)[:10])}"
            )
        by_case: dict[str, dict] = {}
        for item_index, item in enumerate(items):
            case_id = _case_id(item)
            if not case_id:
                standalone.append((raw_group, item, item_index))
                warnings.append(
                    f"{group_name} 第 {item_index + 1} 条缺少 case_id，已回退单组评估"
                )
                continue
            if case_id in duplicates:
                continue
            by_case[case_id] = item
            if case_id not in seen_case_ids:
                seen_case_ids.add(case_id)
                ordered_case_ids.append(case_id)
        normalized_groups.append({
            "group_id": group_id,
            "group_name": group_name,
            "group_role": group_role,
            "dataset_name": str(raw_group.get("dataset_name") or ""),
            "by_case": by_case,
            "total": len(items),
        })

    cases: list[dict] = []
    complete_count = 0
    partial_count = 0
    query_mismatch_count = 0
    fallback_count = 0
    for case_id in ordered_case_ids:
        variants: list[dict] = []
        present_queries: list[str] = []
        case_warnings: list[str] = []
        for group in normalized_groups:
            item = group["by_case"].get(case_id)
            if item is None:
                variants.append({
                    "group_id": group["group_id"],
                    "group_name": group["group_name"],
                    "group_role": group["group_role"],
                    "dataset_name": group["dataset_name"],
                    "availability": "missing",
                    "item": None,
                })
                case_warnings.append(f"{group['group_name']} 缺少该 case")
                continue
            present_queries.append(normalize_query(item.get("query")))
            variants.append({
                "group_id": group["group_id"],
                "group_name": group["group_name"],
                "group_role": group["group_role"],
                "dataset_name": group["dataset_name"],
                "availability": "available",
                "item": item,
            })

        available = [variant for variant in variants if variant["item"] is not None]
        query_mismatch = len(set(present_queries)) > 1
        if query_mismatch:
            strategy = "single_fallback_query_mismatch"
            query_mismatch_count += 1
            case_warnings.append("相同 case_id 的 Query 不一致，已分别回退单组评估")
        elif len(available) >= 2:
            strategy = "multi_group" if len(available) == len(variants) else "multi_group_partial"
            if strategy == "multi_group":
                complete_count += 1
            else:
                partial_count += 1
        else:
            strategy = "single_fallback"
            fallback_count += 1

        first_item = available[0]["item"] if available else {}
        cases.append({
            "id": case_id,
            "case_id": case_id,
            "query": first_item.get("query") or "",
            "context": first_item.get("context") or "",
            "category": "operation",
            "evaluation_strategy": strategy,
            "alignment_status": (
                "query_mismatch" if query_mismatch else
                "complete" if len(available) == len(variants) else
                "partial"
            ),
            "alignment_warnings": case_warnings,
            "group_variants": variants,
        })

    for raw_group, item, item_index in standalone:
        group_id = str(raw_group.get("group_id") or "group").strip()
        group_name = str(raw_group.get("group_name") or group_id).strip()
        fallback_id = f"__single__{group_id}_{item_index + 1}"
        variants = []
        for group in normalized_groups:
            matched = group["group_id"] == group_id
            variants.append({
                "group_id": group["group_id"],
                "group_name": group["group_name"],
                "group_role": group["group_role"],
                "dataset_name": group["dataset_name"],
                "availability": "available" if matched else "missing",
                "item": item if matched else None,
            })
        cases.append({
            "id": fallback_id,
            "case_id": "",
            "query": item.get("query") or "",
            "context": item.get("context") or "",
            "category": "operation",
            "evaluation_strategy": "single_fallback_missing_case_id",
            "alignment_status": "missing_case_id",
            "alignment_warnings": [f"{group_name} 缺少 case_id，已回退单组评估"],
            "group_variants": variants,
        })
        fallback_count += 1

    group_summaries = [
        {
            "group_id": group["group_id"],
            "group_name": group["group_name"],
            "group_role": group["group_role"],
            "dataset_name": group["dataset_name"],
            "total": group["total"],
        }
        for group in normalized_groups
    ]
    return {
        "groups": group_summaries,
        "cases": cases,
        "warnings": warnings,
        "errors": errors,
        "summary": {
            "total_cases": len(cases),
            "complete_cases": complete_count,
            "partial_cases": partial_count,
            "query_mismatch_cases": query_mismatch_count,
            "single_fallback_cases": fallback_count,
        },
    }
