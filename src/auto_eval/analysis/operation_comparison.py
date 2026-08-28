"""任务类结果集对比统计。"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .operation_statistics import (
    OPERATION_CORRECTNESS,
    normalize_operation_issue_types,
    summarize_operation_results,
)


OK_RATE_CLOSE_THRESHOLD = 0.01


def compare_operation_batches(
    batches: list[dict[str, Any]],
    *,
    baseline_task_id: str,
    include_union_rows: bool = False,
) -> dict[str, Any]:
    """比较 2～5 个普通任务类批次。

    全组分布只统计所有批次共有且均有效的 Case；相对对照组指标则使用
    对照组与各实验组各自的共同有效 Case。导出时可额外生成逐题并集。
    """
    if not 2 <= len(batches) <= 5:
        raise ValueError("请选择 2～5 个任务类历史批次")
    task_ids = [str(batch.get("task_id") or "") for batch in batches]
    if not all(task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("历史批次 task_id 不能为空且不能重复")
    if baseline_task_id not in task_ids:
        raise ValueError("对照组必须包含在已选历史批次中")

    _assign_names(batches)
    baseline = next(
        batch for batch in batches if batch["task_id"] == baseline_task_id
    )
    ordered = [baseline] + [
        batch for batch in batches if batch["task_id"] != baseline_task_id
    ]
    baseline["group_label"] = "对照组"
    for index, batch in enumerate(ordered[1:]):
        batch["group_label"] = f"实验组{chr(ord('A') + index)}"

    baseline_rows = list(baseline.get("rows") or [])
    matched_indices: dict[str, dict[int, int]] = {
        baseline_task_id: {index: index for index in range(len(baseline_rows))}
    }
    pairwise: list[dict[str, Any]] = []
    common_positions = set(range(len(baseline_rows)))
    for target in ordered[1:]:
        pair = _compare_pair(baseline, target)
        match_map = pair.pop("_match_map")
        matched_indices[target["task_id"]] = match_map
        common_positions &= set(match_map)
        pairwise.append(pair)

    valid_common_positions = [
        position
        for position in sorted(common_positions)
        if all(
            _is_valid(
                (batch.get("rows") or [])[matched_indices[batch["task_id"]][position]].get("result")
                or {}
            )
            for batch in ordered
        )
    ]
    common_results: dict[str, list[dict[str, Any]]] = {}
    groups: list[dict[str, Any]] = []
    for batch in ordered:
        rows = list(batch.get("rows") or [])
        results = [
            rows[matched_indices[batch["task_id"]][position]].get("result") or {}
            for position in valid_common_positions
        ]
        common_results[batch["task_id"]] = results
        groups.append(_group_statistics(batch, results))

    conclusion_lines = [
        (
            f"所有选中批次共有 {len(common_positions)} 条 Case，其中 "
            f"{len(valid_common_positions)} 条在各批次均有有效评估，"
            "各批次 Correctness 分布按该统一口径计算。"
        )
    ]
    conclusion_lines.extend(pair["conclusion"] for pair in pairwise)
    payload = {
        "schema_version": 2,
        "comparison_type": "operation_history_batches",
        "baseline_task_id": baseline_task_id,
        "baseline_name": _batch_name(baseline),
        "selected_task_ids": [batch["task_id"] for batch in ordered],
        "group_count": len(ordered),
        "ok_rate_close_threshold": OK_RATE_CLOSE_THRESHOLD,
        "groups": groups,
        "all_groups_common_matched_count": len(common_positions),
        "all_groups_common_valid_count": len(valid_common_positions),
        "pairwise": pairwise,
        "conclusion": "\n".join(conclusion_lines),
    }
    if include_union_rows:
        payload["union_rows"] = _build_union_rows(ordered)
    return payload


def _assign_names(batches: list[dict[str, Any]]) -> None:
    raw_names = [
        _dataset_display_name(batch)
        for batch in batches
    ]
    name_counts = Counter(raw_names)
    for batch, raw_name in zip(batches, raw_names):
        batch["comparison_name"] = (
            raw_name
            if name_counts[raw_name] == 1
            else f"{raw_name}（{str(batch['task_id'])[:8]}）"
        )


def _batch_name(batch: dict[str, Any]) -> str:
    if batch.get("comparison_name"):
        return str(batch["comparison_name"])
    return _dataset_display_name(batch)


def _dataset_display_name(batch: dict[str, Any]) -> str:
    """保留数据集原始文件名，包括版本号中的点和文件扩展名。"""
    dataset_name = str(batch.get("dataset_name") or "").replace("\\", "/")
    name = dataset_name.rsplit("/", 1)[-1].strip()
    return name or str(batch.get("task_id") or "未命名批次")


def _group_statistics(
    batch: dict[str, Any],
    common_results: list[dict[str, Any]],
) -> dict[str, Any]:
    statistics = summarize_operation_results(
        common_results,
        total_cases=len(common_results),
    )
    return {
        "task_id": batch["task_id"],
        "dataset_name": batch.get("dataset_name") or "",
        "group_name": _batch_name(batch),
        "group_label": batch.get("group_label") or "",
        "is_baseline": batch.get("group_label") == "对照组",
        "created_at": batch.get("created_at"),
        "judge_provider": batch.get("judge_provider") or "",
        "judge_model": batch.get("judge_model") or "",
        "original_count": len(batch.get("rows") or []),
        "common_valid_count": len(common_results),
        "statistics": statistics,
    }


def _normalize_query(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalize_match_index(value: Any) -> str:
    """规范化数据集 index；数字 1 与表格读取出的 1.0 视为同一值。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return unicodedata.normalize("NFKC", str(value)).strip()


def _unique_match_index(rows: list[dict]) -> dict[str, int]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        value = _normalize_match_index(row.get("index"))
        if value:
            positions[value].append(index)
    return {
        value: indexes[0]
        for value, indexes in positions.items()
        if len(indexes) == 1
    }


def _unique_query_index(
    rows: list[dict],
    excluded: set[int],
) -> dict[str, int]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if index in excluded:
            continue
        query = _normalize_query(row.get("query"))
        if query:
            positions[query].append(index)
    return {
        value: indexes[0]
        for value, indexes in positions.items()
        if len(indexes) == 1
    }


def _match_rows(
    baseline_rows: list[dict],
    target_rows: list[dict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    used_baseline: set[int] = set()
    used_target: set[int] = set()

    baseline_indexes = _unique_match_index(baseline_rows)
    target_indexes = _unique_match_index(target_rows)
    for match_index in sorted(set(baseline_indexes) & set(target_indexes)):
        baseline_index = baseline_indexes[match_index]
        target_index = target_indexes[match_index]
        baseline_query = _normalize_query(baseline_rows[baseline_index].get("query"))
        target_query = _normalize_query(target_rows[target_index].get("query"))
        used_baseline.add(baseline_index)
        used_target.add(target_index)
        if baseline_query and target_query and baseline_query != target_query:
            exclusions.append({
                "reason": "index 相同但 Query 不一致",
                "baseline_index": baseline_index,
                "target_index": target_index,
            })
            continue
        matches.append({
            "baseline_index": baseline_index,
            "target_index": target_index,
            "match_method": "index",
            "match_key": match_index,
        })

    baseline_queries = _unique_query_index(baseline_rows, used_baseline)
    target_queries = _unique_query_index(target_rows, used_target)
    for query_key in sorted(set(baseline_queries) & set(target_queries)):
        baseline_index = baseline_queries[query_key]
        target_index = target_queries[query_key]
        used_baseline.add(baseline_index)
        used_target.add(target_index)
        matches.append({
            "baseline_index": baseline_index,
            "target_index": target_index,
            "match_method": "query",
            "match_key": baseline_rows[baseline_index].get("query") or query_key,
        })
    matches.sort(key=lambda row: int(row["baseline_index"]))
    return matches, exclusions


def _is_valid(result: dict[str, Any]) -> bool:
    return (
        not result.get("error")
        and result.get("correctness") in OPERATION_CORRECTNESS
    )


def _case_issues(result: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(normalize_operation_issue_types(result.get("issue_types"))))


def _paired_issue_rows(
    baseline_results: list[dict[str, Any]],
    target_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    for result in baseline_results:
        baseline_counts.update(_case_issues(result))
    for result in target_results:
        target_counts.update(_case_issues(result))
    denominator = len(baseline_results)
    rows = []
    for issue in set(baseline_counts) | set(target_counts):
        baseline_count = baseline_counts.get(issue, 0)
        target_count = target_counts.get(issue, 0)
        baseline_rate = baseline_count / denominator if denominator else None
        target_rate = target_count / denominator if denominator else None
        rows.append({
            "issue_type": issue,
            "baseline_count": baseline_count,
            "baseline_rate": round(baseline_rate, 4) if baseline_rate is not None else None,
            "target_count": target_count,
            "target_rate": round(target_rate, 4) if target_rate is not None else None,
            "count_delta": target_count - baseline_count,
            "rate_delta": (
                round(target_rate - baseline_rate, 4)
                if target_rate is not None and baseline_rate is not None
                else None
            ),
        })
    # API、Web 和 Excel 使用同一默认顺序：实验组-对照组的
    # 频次差值升序，问题减少（优化）在前，问题增加（劣化）在后。
    rows.sort(key=lambda row: (row["count_delta"], row["issue_type"]))
    return rows


def _compare_pair(baseline: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    baseline_rows = list(baseline.get("rows") or [])
    target_rows = list(target.get("rows") or [])
    matches, _ = _match_rows(baseline_rows, target_rows)
    match_map = {
        int(match["baseline_index"]): int(match["target_index"])
        for match in matches
    }
    valid_pairs = []
    transitions: Counter[tuple[str, str]] = Counter()
    for baseline_index, target_index in match_map.items():
        baseline_result = baseline_rows[baseline_index].get("result") or {}
        target_result = target_rows[target_index].get("result") or {}
        if not (_is_valid(baseline_result) and _is_valid(target_result)):
            continue
        valid_pairs.append((baseline_result, target_result))
        transitions[(
            str(baseline_result["correctness"]),
            str(target_result["correctness"]),
        )] += 1

    denominator = len(valid_pairs)
    baseline_ok = sum(left.get("correctness") == "ok" for left, _ in valid_pairs)
    target_ok = sum(right.get("correctness") == "ok" for _, right in valid_pairs)
    baseline_ok_rate = baseline_ok / denominator if denominator else None
    target_ok_rate = target_ok / denominator if denominator else None
    ok_rate_delta = (
        target_ok_rate - baseline_ok_rate
        if target_ok_rate is not None and baseline_ok_rate is not None
        else None
    )
    if ok_rate_delta is None:
        ok_rate_change = "unavailable"
        ok_rate_change_label = "无有效数据"
    elif ok_rate_delta > OK_RATE_CLOSE_THRESHOLD:
        ok_rate_change = "improved"
        ok_rate_change_label = "优化"
    elif ok_rate_delta < -OK_RATE_CLOSE_THRESHOLD:
        ok_rate_change = "worsened"
        ok_rate_change_label = "劣化"
    else:
        ok_rate_change = "close"
        ok_rate_change_label = "接近"
    to_ok = sum(
        count for (source, destination), count in transitions.items()
        if source != "ok" and destination == "ok"
    )
    from_ok = sum(
        count for (source, destination), count in transitions.items()
        if source == "ok" and destination != "ok"
    )
    baseline_label = str(baseline.get("group_label") or "对照组")
    target_label = str(target.get("group_label") or "实验组")
    if denominator:
        conclusion = (
            f"{target_label} 相对 {baseline_label}：共同有效 {denominator} 条，"
            f"OK 率相差 {ok_rate_delta * 100:+.2f} 个百分点，"
            f"结论为{ok_rate_change_label}；"
            f"{to_ok} 条由其他转为 OK，{from_ok} 条由 OK 转为其他，"
            f"OK 净变化 {to_ok - from_ok:+d} 条。"
        )
    else:
        conclusion = f"{target_label} 与 {baseline_label} 没有双方均有效的共同 Case。"
    return {
        "baseline_task_id": baseline["task_id"],
        "baseline_name": _batch_name(baseline),
        "baseline_label": baseline_label,
        "target_task_id": target["task_id"],
        "target_name": _batch_name(target),
        "target_label": target_label,
        "matched_count": len(matches),
        "valid_pair_count": denominator,
        "baseline_ok_count": baseline_ok,
        "baseline_ok_rate": round(baseline_ok_rate, 4) if baseline_ok_rate is not None else None,
        "target_ok_count": target_ok,
        "target_ok_rate": round(target_ok_rate, 4) if target_ok_rate is not None else None,
        "ok_rate_delta": round(ok_rate_delta, 4) if ok_rate_delta is not None else None,
        "ok_rate_change": ok_rate_change,
        "ok_rate_change_label": ok_rate_change_label,
        "to_ok_count": to_ok,
        "from_ok_count": from_ok,
        "net_ok_change": to_ok - from_ok,
        "issue_type_rows": _paired_issue_rows(
            [left for left, _ in valid_pairs],
            [right for _, right in valid_pairs],
        ),
        "conclusion": conclusion,
        "_match_map": match_map,
    }


def _row_export(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("export"):
        return dict(row["export"])
    result = row.get("result") or {}
    return {
        "index": row.get("index") if row.get("index") is not None else "",
        "item_id": row.get("item_id") or "",
        "case_id": row.get("case_id") or "",
        "query": row.get("query") or "",
        "correctness": result.get("correctness") or "",
        "issue_types": "；".join(_case_issues(result)),
        "is_low_level": result.get("is_low_level") or "",
        "rationale": result.get("rationale") or "",
        "error": result.get("error") or "",
    }


def _row_union_key(row: dict[str, Any]) -> str:
    match_index = _normalize_match_index(row.get("index"))
    query = str(row.get("query") or "").strip()
    return match_index or query


def _find_nonbaseline_union_entry(
    entries: list[dict[str, Any]],
    row: dict[str, Any],
    task_id: str,
) -> dict[str, Any] | None:
    candidates = [
        entry for entry in entries
        if not entry["has_baseline"] and task_id not in entry["group_rows"]
    ]
    match_index = _normalize_match_index(row.get("index"))
    query = _normalize_query(row.get("query"))
    if match_index:
        by_index = [
            entry for entry in candidates
            if _normalize_match_index(entry["representative"].get("index")) == match_index
            and (
                not query
                or not _normalize_query(entry["representative"].get("query"))
                or query == _normalize_query(entry["representative"].get("query"))
            )
        ]
        if len(by_index) == 1:
            return by_index[0]
    if query:
        by_query = [
            entry for entry in candidates
            if _normalize_query(entry["representative"].get("query")) == query
        ]
        if len(by_query) == 1:
            return by_query[0]
    return None


def _build_union_rows(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = batches[0]
    baseline_rows = list(baseline.get("rows") or [])
    entries = [
        {
            "match_key": _row_union_key(row),
            "representative": row,
            "has_baseline": True,
            "group_rows": {baseline["task_id"]: _row_export(row)},
        }
        for row in baseline_rows
    ]
    for target in batches[1:]:
        target_rows = list(target.get("rows") or [])
        matches, _ = _match_rows(baseline_rows, target_rows)
        matched_target: set[int] = set()
        for match in matches:
            baseline_index = int(match["baseline_index"])
            target_index = int(match["target_index"])
            matched_target.add(target_index)
            entries[baseline_index]["group_rows"][target["task_id"]] = _row_export(
                target_rows[target_index]
            )
        for index, row in enumerate(target_rows):
            if index in matched_target:
                continue
            entry = _find_nonbaseline_union_entry(entries, row, target["task_id"])
            if entry is None:
                entry = {
                    "match_key": _row_union_key(row),
                    "representative": row,
                    "has_baseline": False,
                    "group_rows": {},
                }
                entries.append(entry)
            entry["group_rows"][target["task_id"]] = _row_export(row)
    group_count = len(batches)
    return [
        {
            "match_key": entry["match_key"],
            "present_group_count": len(entry["group_rows"]),
            "all_groups_present": len(entry["group_rows"]) == group_count,
            "group_rows": entry["group_rows"],
        }
        for entry in entries
    ]
