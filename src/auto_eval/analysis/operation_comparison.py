"""任务类结果集对比统计。"""
from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .operation_statistics import (
    OPERATION_CORRECTNESS,
    normalize_operation_issue_types,
    summarize_operation_results,
)
from ..judges.operation_fields import LEGACY_REVIEW_CATEGORIES, split_operation_issue_fields


OK_RATE_CLOSE_THRESHOLD = 0.01


def compare_operation_batches(
    batches: list[dict[str, Any]],
    *,
    baseline_task_id: str,
    include_union_rows: bool = False,
    scope: str = "all_valid",
) -> dict[str, Any]:
    """比较 2～5 个普通任务类批次。

    全组分布只统计所有批次共有且均有效的 Case；相对对照组指标则使用
    对照组与各实验组各自的共同有效 Case。common_decidable 口径下，
    效果指标统一使用全组均为 OK/NOK 的交集；复核类别独立使用双方
    共同有效集（含 no_support）。导出时可额外生成逐题并集。
    """
    if scope not in {"all_valid", "common_decidable"}:
        raise ValueError("未知的对比统计口径")
    if not 2 <= len(batches) <= 5:
        raise ValueError("请选择 2～5 个任务类历史批次")
    task_ids = [str(batch.get("task_id") or "") for batch in batches]
    if not all(task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("历史批次 task_id 不能为空且不能重复")
    if baseline_task_id not in task_ids:
        raise ValueError("对照组必须包含在已选历史批次中")

    batches = _normalized_batches(batches)

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

    alignment_warnings = [
        {
            **warning,
            "baseline_task_id": pair["baseline_task_id"],
            "baseline_label": pair["baseline_label"],
            "target_task_id": pair["target_task_id"],
            "target_label": pair["target_label"],
        }
        for pair in pairwise
        for warning in pair.get("alignment_warnings") or []
    ]
    alignment_summary = {
        "matched": len(common_positions),
        "id_matched": sum(
            int((pair.get("alignment_summary") or {}).get("id_matched") or 0)
            for pair in pairwise
        ),
        "query_matched": sum(
            int((pair.get("alignment_summary") or {}).get("query_matched") or 0)
            for pair in pairwise
        ),
        "query_mismatch": sum(
            warning.get("type") == "query_mismatch"
            for warning in alignment_warnings
        ),
        "missing_id": sum(
            not _row_item_id(row)
            for batch in ordered
            for row in (batch.get("rows") or [])
        ),
        "duplicate_id": sum(
            len(indexes) > 1
            for batch in ordered
            for indexes in _item_id_positions(batch.get("rows") or []).values()
        ),
        "unmatched": sum(
            warning.get("type") == "unmatched_id"
            for warning in alignment_warnings
        ),
        "duplicate_query": sum(
            warning.get("type") == "duplicate_query"
            for warning in alignment_warnings
        ),
        "id_conflict": sum(
            warning.get("type") == "id_conflict"
            for warning in alignment_warnings
        ),
    }

    valid_common_positions = [
        position
        for position in sorted(common_positions)
        if all(
            _is_valid(
                (batch.get("rows") or [])[matched_indices[batch["task_id"]][position]].get("result")
                or {}
            )
            and (scope == "all_valid" or (
                (batch.get("rows") or [])[matched_indices[batch["task_id"]][position]].get("result") or {}
            ).get("correctness") in {"ok", "nok"})
            for batch in ordered
        )
    ]
    if scope == "common_decidable":
        all_valid_pairs = {pair["target_task_id"]: pair for pair in pairwise}
        pairwise = []
        for target in ordered[1:]:
            pair = _compare_pair(baseline, target, allowed_positions=set(valid_common_positions))
            # 复核原因始终看双方共同有效集，不随效果口径排除 no_support。
            full_pair = all_valid_pairs[target["task_id"]]
            for key in (
                "review_category_rows", "review_valid_pair_count",
                "baseline_review_case_count", "target_review_case_count",
            ):
                pair[key] = full_pair[key]
            pair.pop("_match_map")
            pairwise.append(pair)
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
            f"{len(valid_common_positions)} 条在各批次均有有效评估"
            + ("且均为 OK/NOK，" if scope == "common_decidable" else "，") +
            "各批次 Correctness 分布按该统一口径计算。"
        )
    ]
    conclusion_lines.extend(pair["conclusion"] for pair in pairwise)
    payload = {
        "schema_version": 2,
        "scope": scope,
        "common_positions": valid_common_positions,
        "comparison_type": "operation_history_batches",
        "baseline_task_id": baseline_task_id,
        "baseline_name": _batch_name(baseline),
        "selected_task_ids": [batch["task_id"] for batch in ordered],
        "group_count": len(ordered),
        "ok_rate_close_threshold": OK_RATE_CLOSE_THRESHOLD,
        "groups": groups,
        "all_groups_common_matched_count": len(common_positions),
        "all_groups_common_valid_count": len(valid_common_positions),
        "alignment_summary": alignment_summary,
        "alignment_warnings": alignment_warnings,
        "pairwise": pairwise,
        "conclusion": "\n".join(conclusion_lines),
    }
    if include_union_rows:
        payload["union_rows"] = _build_union_rows(ordered)
    return payload


def _normalized_batches(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """统计和报告共享旧字段兼容，只修改内存副本。"""
    batches = copy.deepcopy(batches)
    for batch in batches:
        for row in batch.get("rows") or []:
            result = row.get("result") or {}
            if result.get("correctness") == "others":
                result["judge_correctness"] = "others"
                result["correctness"] = "no_support"
            effect, review = split_operation_issue_fields(
                normalize_operation_issue_types(result.get("issue_types")),
                LEGACY_REVIEW_CATEGORIES,
            )
            result["issue_types"] = effect
            result["need_review_cate"] = list(dict.fromkeys([
                *normalize_operation_issue_types(result.get("need_review_cate")), *review,
            ]))
    return batches


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
        "source_type": "upload" if batch.get("source_type") == "upload" else "history",
        "source_label": "上传结果" if batch.get("source_type") == "upload" else "原生评估",
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


def _normalize_item_id(value: Any) -> str:
    """规范化显式业务 ID；不把内部数字 index 当作对齐键。"""
    if value is None or isinstance(value, bool):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _row_item_id(row: dict[str, Any]) -> str:
    return _normalize_item_id(row.get("id") or row.get("item_id"))


def _item_id_positions(rows: list[dict]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        value = _row_item_id(row)
        if value:
            positions[value].append(index)
    return positions


def _match_rows(
    baseline_rows: list[dict],
    target_rows: list[dict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """优先按显式 ID 对齐；至少一侧缺少 ID 时按唯一 Query 兜底。"""
    matches: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    baseline_ids = _item_id_positions(baseline_rows)
    target_ids = _item_id_positions(target_rows)
    blocked_baseline: set[int] = set()
    blocked_target: set[int] = set()

    for index, row in enumerate(baseline_rows):
        if not _row_item_id(row):
            warnings.append({
                "type": "missing_id",
                "side": "baseline",
                "baseline_index": index,
                "message": "对照组存在缺少 id 的数据，将尝试按唯一 Query 兜底对齐",
            })
    for index, row in enumerate(target_rows):
        if not _row_item_id(row):
            warnings.append({
                "type": "missing_id",
                "side": "target",
                "target_index": index,
                "message": "实验组存在缺少 id 的数据，将尝试按唯一 Query 兜底对齐",
            })
    for side, positions in (("baseline", baseline_ids), ("target", target_ids)):
        for item_id, indexes in positions.items():
            if len(indexes) > 1:
                (blocked_baseline if side == "baseline" else blocked_target).update(indexes)
                warnings.append({
                    "type": "duplicate_id",
                    "side": side,
                    "id": item_id,
                    "positions": indexes,
                    "message": f"{side == 'baseline' and '对照组' or '实验组'} id={item_id} 重复，已排除对齐",
                })

    unique_baseline = {
        item_id: indexes[0]
        for item_id, indexes in baseline_ids.items()
        if len(indexes) == 1
    }
    unique_target = {
        item_id: indexes[0]
        for item_id, indexes in target_ids.items()
        if len(indexes) == 1
    }
    for item_id in sorted(set(unique_baseline) & set(unique_target)):
        baseline_index = unique_baseline[item_id]
        target_index = unique_target[item_id]
        blocked_baseline.add(baseline_index)
        blocked_target.add(target_index)
        baseline_query = _normalize_query(baseline_rows[baseline_index].get("query"))
        target_query = _normalize_query(target_rows[target_index].get("query"))
        if baseline_query != target_query:
            warnings.append({
                "type": "query_mismatch",
                "id": item_id,
                "baseline_index": baseline_index,
                "target_index": target_index,
                "baseline_query": str(baseline_rows[baseline_index].get("query") or ""),
                "target_query": str(target_rows[target_index].get("query") or ""),
                "message": f"id={item_id} 在不同数据集中的 Query 不一致，已排除统计",
            })
            continue
        matches.append({
            "baseline_index": baseline_index,
            "target_index": target_index,
            "match_method": "id",
            "match_key": item_id,
        })

    baseline_queries: dict[str, list[int]] = defaultdict(list)
    target_queries: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(baseline_rows):
        if index in blocked_baseline:
            continue
        query = _normalize_query(row.get("query"))
        if query:
            baseline_queries[query].append(index)
    for index, row in enumerate(target_rows):
        if index in blocked_target:
            continue
        query = _normalize_query(row.get("query"))
        if query:
            target_queries[query].append(index)

    query_blocked_baseline: set[int] = set()
    query_blocked_target: set[int] = set()
    for query_key in sorted(set(baseline_queries) & set(target_queries)):
        baseline_indexes = baseline_queries[query_key]
        target_indexes = target_queries[query_key]
        if len(baseline_indexes) != 1 or len(target_indexes) != 1:
            query_blocked_baseline.update(baseline_indexes)
            query_blocked_target.update(target_indexes)
            warnings.append({
                "type": "duplicate_query",
                "query": str(
                    baseline_rows[baseline_indexes[0]].get("query")
                    or target_rows[target_indexes[0]].get("query")
                    or ""
                ),
                "baseline_count": len(baseline_indexes),
                "target_count": len(target_indexes),
                "message": (
                    "Query 在对照组或实验组中重复，无法唯一对齐："
                    f"对照组 {len(baseline_indexes)} 条，实验组 {len(target_indexes)} 条"
                ),
            })
            continue
        baseline_index = baseline_indexes[0]
        target_index = target_indexes[0]
        baseline_id = _row_item_id(baseline_rows[baseline_index])
        target_id = _row_item_id(target_rows[target_index])
        if baseline_id and target_id:
            query_blocked_baseline.add(baseline_index)
            query_blocked_target.add(target_index)
            warnings.append({
                "type": "id_conflict",
                "baseline_id": baseline_id,
                "target_id": target_id,
                "baseline_index": baseline_index,
                "target_index": target_index,
                "query": str(baseline_rows[baseline_index].get("query") or ""),
                "message": (
                    f"Query 相同但两侧 id 不同（{baseline_id} / {target_id}），"
                    "未使用 Query 兜底"
                ),
            })
            continue
        matches.append({
            "baseline_index": baseline_index,
            "target_index": target_index,
            "match_method": "query",
            "match_key": baseline_rows[baseline_index].get("query") or query_key,
        })

    matched_baseline = {int(match["baseline_index"]) for match in matches}
    matched_target = {int(match["target_index"]) for match in matches}
    suppressed_baseline = blocked_baseline | query_blocked_baseline | matched_baseline
    suppressed_target = blocked_target | query_blocked_target | matched_target
    for item_id in sorted(set(unique_baseline) - set(unique_target)):
        baseline_index = unique_baseline[item_id]
        if baseline_index in suppressed_baseline:
            continue
        warnings.append({
            "type": "unmatched_id",
            "side": "baseline",
            "id": item_id,
            "baseline_index": baseline_index,
            "message": f"id={item_id} 仅存在于对照组",
        })
    for item_id in sorted(set(unique_target) - set(unique_baseline)):
        target_index = unique_target[item_id]
        if target_index in suppressed_target:
            continue
        warnings.append({
            "type": "unmatched_id",
            "side": "target",
            "id": item_id,
            "target_index": target_index,
            "message": f"id={item_id} 仅存在于实验组",
        })
    matches.sort(key=lambda row: int(row["baseline_index"]))
    return matches, warnings


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
    baseline_decidable = [result for result in baseline_results if result.get("correctness") in {"ok", "nok"}]
    target_decidable = [result for result in target_results if result.get("correctness") in {"ok", "nok"}]
    for result in baseline_decidable:
        baseline_counts.update(_case_issues(result))
    for result in target_decidable:
        target_counts.update(_case_issues(result))
    rows = []
    for issue in set(baseline_counts) | set(target_counts):
        baseline_count = baseline_counts.get(issue, 0)
        target_count = target_counts.get(issue, 0)
        baseline_rate = baseline_count / len(baseline_decidable) if baseline_decidable else None
        target_rate = target_count / len(target_decidable) if target_decidable else None
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


def _paired_review_rows(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    counts = [Counter(), Counter()]
    for pair in pairs:
        for side, result in enumerate(pair):
            counts[side].update(set(normalize_operation_issue_types(result.get("need_review_cate"))))
    denominator = len(pairs)
    rows = []
    for category in counts[0].keys() | counts[1].keys():
        baseline, target = counts[0][category], counts[1][category]
        rows.append({
            "category": category,
            "baseline_count": baseline,
            "target_count": target,
            "baseline_rate": round(baseline / denominator, 4) if denominator else None,
            "target_rate": round(target / denominator, 4) if denominator else None,
            "count_delta": target - baseline,
            "rate_delta": round((target - baseline) / denominator, 4) if denominator else None,
        })
    return sorted(rows, key=lambda row: (-row["target_count"], row["category"]))


def _compare_pair(
    baseline: dict[str, Any], target: dict[str, Any],
    *, allowed_positions: set[int] | None = None,
) -> dict[str, Any]:
    baseline_rows = list(baseline.get("rows") or [])
    target_rows = list(target.get("rows") or [])
    matches, alignment_warnings = _match_rows(baseline_rows, target_rows)
    match_map = {
        int(match["baseline_index"]): int(match["target_index"])
        for match in matches
    }
    valid_pairs = []
    transitions: Counter[tuple[str, str]] = Counter()
    for baseline_index, target_index in match_map.items():
        if allowed_positions is not None and baseline_index not in allowed_positions:
            continue
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
    baseline_nok = sum(left.get("correctness") == "nok" for left, _ in valid_pairs)
    target_ok = sum(right.get("correctness") == "ok" for _, right in valid_pairs)
    target_nok = sum(right.get("correctness") == "nok" for _, right in valid_pairs)
    baseline_rate_denominator = baseline_ok + baseline_nok
    target_rate_denominator = target_ok + target_nok
    baseline_ok_rate = (
        baseline_ok / baseline_rate_denominator
        if baseline_rate_denominator else None
    )
    baseline_nok_rate = (
        baseline_nok / baseline_rate_denominator
        if baseline_rate_denominator else None
    )
    target_ok_rate = (
        target_ok / target_rate_denominator
        if target_rate_denominator else None
    )
    target_nok_rate = (
        target_nok / target_rate_denominator
        if target_rate_denominator else None
    )
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
    if denominator and ok_rate_delta is not None:
        conclusion = (
            f"{target_label} 相对 {baseline_label}：共同有效 {denominator} 条，"
            f"OK/NOK 率分母分别为 {target_rate_denominator}/{baseline_rate_denominator} 条，"
            f"OK 率相差 {ok_rate_delta * 100:+.2f} 个百分点，"
            f"结论为{ok_rate_change_label}；"
            f"{to_ok} 条由其他转为 OK，{from_ok} 条由 OK 转为其他，"
            f"OK 净变化 {to_ok - from_ok:+d} 条。"
        )
    elif not denominator:
        conclusion = f"{target_label} 与 {baseline_label} 没有双方均有效的共同 Case。"
    else:
        conclusion = (
            f"{target_label} 与 {baseline_label} 有 {denominator} 条共同有效 Case，"
            "但至少一组没有 OK 或 NOK，无法计算 OK/NOK 率差值。"
        )
    if allowed_positions is not None:
        conclusion = conclusion.replace("其他", "NOK")
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
        "baseline_nok_count": baseline_nok,
        "baseline_rate_denominator": baseline_rate_denominator,
        "baseline_ok_rate": round(baseline_ok_rate, 4) if baseline_ok_rate is not None else None,
        "baseline_nok_rate": round(baseline_nok_rate, 4) if baseline_nok_rate is not None else None,
        "target_ok_count": target_ok,
        "target_nok_count": target_nok,
        "target_rate_denominator": target_rate_denominator,
        "target_ok_rate": round(target_ok_rate, 4) if target_ok_rate is not None else None,
        "target_nok_rate": round(target_nok_rate, 4) if target_nok_rate is not None else None,
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
        "review_category_rows": _paired_review_rows(valid_pairs),
        "review_valid_pair_count": denominator,
        "baseline_review_case_count": sum(bool(left.get("need_review_cate")) for left, _ in valid_pairs),
        "target_review_case_count": sum(bool(right.get("need_review_cate")) for _, right in valid_pairs),
        "alignment_summary": {
            "matched": len(matches),
            "id_matched": sum(
                match.get("match_method") == "id" for match in matches
            ),
            "query_matched": sum(
                match.get("match_method") == "query" for match in matches
            ),
            "query_mismatch": sum(
                warning.get("type") == "query_mismatch"
                for warning in alignment_warnings
            ),
            "missing_id": sum(
                warning.get("type") == "missing_id"
                for warning in alignment_warnings
            ),
            "duplicate_id": sum(
                warning.get("type") == "duplicate_id"
                for warning in alignment_warnings
            ),
            "unmatched": sum(
                warning.get("type") == "unmatched_id"
                for warning in alignment_warnings
            ),
            "duplicate_query": sum(
                warning.get("type") == "duplicate_query"
                for warning in alignment_warnings
            ),
            "id_conflict": sum(
                warning.get("type") == "id_conflict"
                for warning in alignment_warnings
            ),
        },
        "alignment_warnings": alignment_warnings,
        "conclusion": conclusion,
        "_match_map": match_map,
    }


def _row_export(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("export"):
        exported = dict(row["export"])
        exported["id"] = _row_item_id(row)
        return exported
    result = row.get("result") or {}
    return {
        "id": _row_item_id(row),
        "index": row.get("index") if row.get("index") is not None else "",
        "item_id": row.get("item_id") or "",
        "case_id": row.get("case_id") or "",
        "query": row.get("query") or "",
        "correctness": result.get("correctness") or "",
        "issue_types": "；".join(_case_issues(result)),
        "need_review_cate": "；".join(normalize_operation_issue_types(result.get("need_review_cate"))),
        "card_presence": result.get("card_presence") or "",
        "card_types": "；".join(normalize_operation_issue_types(result.get("card_types"))),
        "card_suitability": result.get("card_suitability") or "",
        "card_suitability_reason": result.get("card_suitability_reason") or "",
        "is_low_level": result.get("is_low_level") or "",
        "rationale": result.get("rationale") or "",
        "error": result.get("error") or "",
    }


def _row_union_key(row: dict[str, Any]) -> str:
    return _row_item_id(row) or str(row.get("query") or "").strip()


def _find_nonbaseline_union_entry(
    entries: list[dict[str, Any]],
    row: dict[str, Any],
    task_id: str,
) -> dict[str, Any] | None:
    candidates = [
        entry for entry in entries
        if not entry["has_baseline"] and task_id not in entry["group_rows"]
    ]
    item_id = _row_item_id(row)
    query = _normalize_query(row.get("query"))
    if item_id:
        by_id = [
            entry for entry in candidates
            if _row_item_id(entry["representative"]) == item_id
            and query == _normalize_query(entry["representative"].get("query"))
        ]
        if len(by_id) == 1:
            return by_id[0]
    if query:
        by_query = [
            entry for entry in candidates
            if _normalize_query(entry["representative"].get("query")) == query
            and (
                not item_id
                or not _row_item_id(entry["representative"])
            )
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
