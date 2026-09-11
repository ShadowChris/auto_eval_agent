"""dev_czm 报告同步：统计 / 单批报告投影 / 批次对比 / 离线 HTML 冒烟。"""
from __future__ import annotations

from pathlib import Path

import pytest

from auto_eval.analysis.operation_report import CASE_DETAIL_FIELDS
from auto_eval.report.operation import build_operation_report_html
from auto_eval.web.report_payload import comparison_report, single_report


def _snapshot(task_id: str, rows: list[dict]) -> dict:
    items = [{"id": str(row.get("_id", f"q{i}"))} for i, row in enumerate(rows)]
    results = []
    for i, row in enumerate(rows):
        result_row = {"index": i, "item_id": row.get("_id", f"q{i}"), "query": row.get("_q", "")}
        for key, value in row.items():
            if not key.startswith("_"):
                result_row[key] = value
        results.append(result_row)
    return {
        "task_id": task_id,
        "dataset_name": f"{task_id}.json",
        "mode": "rich_content",
        "status": "done",
        "items": items,
        "results": results,
    }


def _batch_a():
    return _snapshot("tz1", [
        {"_id": "q0", "_q": "天气", "problem_solved": "ok"},
        {"_id": "q1", "_q": "闹钟", "problem_solved": "nok", "answer_issues": "错误1:没设\n错误2:报错"},
        {"_id": "q2", "_q": "音乐", "problem_solved": "need_review", "answer_issues": "错误1:不清楚"},
        {"_id": "q3", "_q": "失败", "error": "Timeout"},
    ])


def test_statistics_denominator_and_classes():
    stats = single_report(_batch_a())["statistics"]
    assert stats["total_cases"] == 4
    assert stats["valid_count"] == 3
    assert stats["failed_count"] == 1
    assert stats["pending_count"] == 0
    assert stats["ok_count"] == 1
    assert stats["nok_count"] == 1
    assert stats["ok_rate"] == pytest.approx(1 / 3, abs=1e-4)  # 负载四舍五入到 4 位
    correctness = {row["correctness"]: row["count"] for row in stats["correctness_rows"]}
    assert correctness == {"ok": 1, "nok": 1, "need_review": 1}


def test_statistics_issue_types_deduped_per_case():
    stats = single_report(_batch_a())["statistics"]
    rows = {row["issue_type"]: row["case_count"] for row in stats["issue_type_rows"]}
    assert rows == {  # 错误1 只在 q1、q2 各计一次
        "错误1": 2,
        "错误2": 1,
    }


def test_single_report_cases_only_valid_and_whitelisted():
    rep = single_report(_batch_a())
    assert rep["kind"] == "single"
    assert rep["schema_version"] == 1
    assert len(rep["cases"]) == 3  # 失败行不进入 Case 池
    ids = {case["item_id"] for case in rep["cases"]}
    assert ids == {"q0", "q1", "q2"}
    case = next(case for case in rep["cases"] if case["item_id"] == "q1")
    assert case["correctness"] == "nok"
    assert case["issue_types"] == ["错误1", "错误2"]
    # 投影只含白名单字段，不含原始错误/帧数据
    assert "error" not in case
    assert "traceback" not in case
    assert set(case.keys()) & set(CASE_DETAIL_FIELDS)  # 至少带详情字段


def test_comparison_pairing_and_report():
    comparison = comparison_report(
        [_batch_a(), _snapshot("tz2", [
            {"_id": "q0", "_q": "天气", "problem_solved": "ok"},
            {"_id": "q1", "_q": "闹钟", "problem_solved": "nok", "answer_issues": "错误1:没设"},
            {"_id": "q2", "_q": "音乐", "problem_solved": "ok"},
        ])],
        baseline_task_id="tz1",
    )
    assert comparison["group_count"] == 2
    assert comparison["all_groups_common_matched_count"] == 3
    assert comparison["all_groups_common_valid_count"] == 3
    pair = comparison["pairwise"][0]
    # tz1: q0→ok,q1→nok,q2→review(1/3 ok)；tz2: q0→ok,q1→nok,q2→ok(2/3 ok)
    assert pair["baseline_ok_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert pair["target_ok_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert pair["ok_rate_change"] == "improved"
    report = comparison["report"]
    assert report["kind"] == "comparison"
    assert len(report["pairs"]) == 1
    assert report["pairs"][0]["matches"] == [[0, 0], [1, 1], [2, 2]]


def test_offline_html_smoke():
    payload = single_report(_batch_a())
    html = build_operation_report_html(payload).decode("utf-8")
    assert "Content-Security-Policy" in html
    assert "AutoEvalOperationReport.mount" in html
    assert "<title>垂域评估报告</title>" in html
    assert "operation-report-data" in html


def test_comparison_html_title():
    comparison = comparison_report(
        [_batch_a(), _snapshot("tz2", [{"_id": "q0", "_q": "天气", "problem_solved": "ok"}])],
        baseline_task_id="tz1",
    )
    html = build_operation_report_html(comparison["report"]).decode("utf-8")
    assert "<title>垂域对比分析报告</title>" in html


def test_report_rejects_non_rich_content():
    from auto_eval.web.report_payload import single_report as sr

    snapshot = _snapshot("tz1", [{"_id": "q0", "_q": "x", "problem_solved": "ok"}])
    snapshot["mode"] = "compare"
    with pytest.raises(ValueError):
        sr(snapshot)


def test_assets_render_js_and_css_exist():
    assets = Path(__file__).resolve().parents[1] / "src" / "auto_eval" / "report" / "assets"
    assert (assets / "operation_report.js").exists()
    assert (assets / "operation_report.css").exists()