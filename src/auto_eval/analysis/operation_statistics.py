"""任务类单批结果统计。

统计逻辑保持为纯函数，供 Web 汇总与 Excel 导出共用，避免不同展示层
各自计算后产生口径差异。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable


OPERATION_CORRECTNESS = ("ok", "nok", "no_support", "others")


def _issue_types(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[；;,，]", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def summarize_operation_results(
    results: Iterable[dict[str, Any]],
    *,
    total_cases: int | None = None,
) -> dict[str, Any]:
    """汇总一批普通任务类结果。

    OK 率和各类占比只以具有合法 correctness 的有效评估 Case 为分母；
    运行错误与尚无合法判定的条目分别计入失败、待评估，不伪装成 nok。
    每个 issue type 在同一 Case 中最多计一次。
    """
    rows = list(results)
    total = max(int(total_cases if total_cases is not None else len(rows)), 0)
    failed = sum(1 for row in rows if row.get("error"))
    valid = [
        row
        for row in rows
        if not row.get("error") and row.get("correctness") in OPERATION_CORRECTNESS
    ]
    valid_count = len(valid)
    pending = max(total - valid_count - failed, 0)
    correctness_counts = Counter(row["correctness"] for row in valid)

    correctness_rows = [
        {
            "correctness": correctness,
            "count": correctness_counts.get(correctness, 0),
            "rate": (
                round(correctness_counts.get(correctness, 0) / valid_count, 4)
                if valid_count
                else None
            ),
        }
        for correctness in OPERATION_CORRECTNESS
    ]

    issue_case_counts: Counter[str] = Counter()
    issue_case_count = 0
    for row in valid:
        raw_issues = _issue_types(row.get("issue_types"))
        unique_issues = list(dict.fromkeys(raw_issues))
        if unique_issues:
            issue_case_count += 1
        for issue in unique_issues:
            issue_case_counts[issue] += 1

    issue_type_rows = [
        {
            "issue_type": issue,
            "case_count": count,
            "rate": round(count / valid_count, 4) if valid_count else None,
        }
        for issue, count in sorted(
            issue_case_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    ok_count = correctness_counts.get("ok", 0)
    nok_count = correctness_counts.get("nok", 0)
    coverage_rate = round(valid_count / total, 4) if total else None
    ok_rate = round(ok_count / valid_count, 4) if valid_count else None
    conclusion = _operation_statistics_conclusion(
        total=total,
        valid_count=valid_count,
        failed=failed,
        pending=pending,
        correctness_counts=correctness_counts,
        issue_type_rows=issue_type_rows,
    )
    return {
        "total_cases": total,
        "valid_count": valid_count,
        "failed_count": failed,
        "pending_count": pending,
        "coverage_rate": coverage_rate,
        "ok_count": ok_count,
        "nok_count": nok_count,
        "ok_rate_denominator": valid_count,
        "ok_rate": ok_rate,
        "issue_case_count": issue_case_count,
        "correctness_rows": correctness_rows,
        "issue_type_rows": issue_type_rows,
        "conclusion": conclusion,
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _operation_statistics_conclusion(
    *,
    total: int,
    valid_count: int,
    failed: int,
    pending: int,
    correctness_counts: Counter[str],
    issue_type_rows: list[dict[str, Any]],
) -> str:
    if not total:
        return "当前数据集没有可统计的 Case。"
    if not valid_count:
        suffix = []
        if failed:
            suffix.append(f"{failed} 条评估失败")
        if pending:
            suffix.append(f"{pending} 条待评估")
        detail = "，".join(suffix) or "尚未产生评估结果"
        return f"本批共 {total} 条，暂无有效判定；{detail}。"

    ok_count = correctness_counts.get("ok", 0)
    nok_count = correctness_counts.get("nok", 0)
    non_ok = [
        (correctness, correctness_counts.get(correctness, 0))
        for correctness in OPERATION_CORRECTNESS[1:]
        if correctness_counts.get(correctness, 0)
    ]
    parts = [
        f"本批共 {total} 条，{valid_count} 条获得有效判定",
        f"评估覆盖率 {_percent(valid_count / total if total else None)}",
        (
            f"OK {ok_count} 条、NOK {nok_count} 条，"
            f"OK 率 {_percent(ok_count / valid_count)}"
            f"（分母为有效评估数 {valid_count}）"
        ),
    ]
    if non_ok:
        main_correctness, main_count = sorted(
            non_ok,
            key=lambda item: (-item[1], OPERATION_CORRECTNESS.index(item[0])),
        )[0]
        parts.append(f"非 OK 结果主要为 {main_correctness}（{main_count} 条）")
    if issue_type_rows:
        top_issue = issue_type_rows[0]
        parts.append(
            f"最高频问题为“{top_issue['issue_type']}”（涉及 {top_issue['case_count']} 条）"
        )
    if failed:
        parts.append(
            f"另有 {failed} 条评估失败，已从有效评估数据及相关指标计算中排除"
        )
    if pending:
        parts.append(f"另有 {pending} 条待评估")
    return "；".join(parts) + "。"
