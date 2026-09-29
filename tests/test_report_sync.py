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
        {"_id": "q1", "_q": "闹钟", "problem_solved": "nok", "answer_issues": "执行/操作失败:没设提醒\n回答矛盾:前后矛盾"},
        {"_id": "q2", "_q": "音乐", "problem_solved": "need_review", "answer_issues": "执行/操作失败:不清楚"},
        {"_id": "q3", "_q": "失败", "error": "Timeout"},
    ])


def test_statistics_denominator_and_classes():
    stats = single_report(_batch_a())["statistics"]
    assert stats["total_cases"] == 4
    # 有效评估只计 ok/nok：q0(ok)+q1(nok)；q2(need_review→no_support) 排除
    assert stats["valid_count"] == 2
    assert stats["failed_count"] == 1
    assert stats["pending_count"] == 0
    assert stats["ok_count"] == 1
    assert stats["nok_count"] == 1
    assert stats["ok_rate"] == pytest.approx(0.5, abs=1e-4)
    correctness = {row["correctness"]: row["count"] for row in stats["correctness_rows"]}
    assert correctness == {"ok": 1, "nok": 1, "no_support": 1}
    # no_support 行不进有效分母（rate 为 None）
    no_support = next(r for r in stats["correctness_rows"] if r["correctness"] == "no_support")
    assert no_support["rate"] is None


def test_statistics_issue_types_deduped_per_case():
    stats = single_report(_batch_a())["statistics"]
    rows = {row["issue_type"]: row["case_count"] for row in stats["issue_type_rows"]}
    # 问题统计只计入有效评估(ok/nok= q0,q1)；q2 为 no_support 不计入
    assert rows == {
        "执行/操作失败": 1,
        "回答矛盾": 1,
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
    assert case["issue_types"] == ["执行/操作失败", "回答矛盾"]
    # need_review 的 case 在单批报告展示为 no_support、valid=false
    excluded = next(case for case in rep["cases"] if case["item_id"] == "q2")
    assert excluded["correctness"] == "no_support"
    assert excluded["valid"] is False
    # 投影只含白名单字段，不含原始错误/帧数据
    assert "error" not in case
    assert "traceback" not in case
    assert set(case.keys()) & set(CASE_DETAIL_FIELDS)  # 至少带详情字段


def test_comparison_pairing_and_report():
    comparison = comparison_report(
        [_batch_a(), _snapshot("tz2", [
            {"_id": "q0", "_q": "天气", "problem_solved": "ok"},
            {"_id": "q1", "_q": "闹钟", "problem_solved": "ok"},
            {"_id": "q2", "_q": "音乐", "problem_solved": "ok"},
        ])],
        baseline_task_id="tz1",
    )
    assert comparison["group_count"] == 2
    # 3 条都按 index 匹配上
    assert comparison["all_groups_common_matched_count"] == 3
    # 共同有效排除 need_review（baseline q2 为 need_review）→ 只剩 q0、q1
    assert comparison["all_groups_common_valid_count"] == 2
    pair = comparison["pairwise"][0]
    assert pair["valid_pair_count"] == 2
    # q0、q1：baseline=ok,nok → 1/2 ok；target=ok,ok → 1/1 ok
    assert pair["baseline_ok_rate"] == pytest.approx(0.5, abs=1e-4)
    assert pair["target_ok_rate"] == pytest.approx(1.0, abs=1e-4)
    assert pair["ok_rate_change"] == "improved"
    report = comparison["report"]
    assert report["kind"] == "comparison"
    assert len(report["pairs"]) == 1
    # 对比报告配对也只含 q0、q1（q2 因 need_review 被排除）
    assert report["pairs"][0]["matches"] == [[0, 0], [1, 1]]


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


def test_issue_labels_whitelist_keeps_enum_members():
    from auto_eval.web.report_payload import _issue_labels

    # 枚举内标签原样保留、同题去重
    labels = _issue_labels("回答内容不相关：跑了题\n挂卡/资源缺失：缺卡\n回答内容不相关：重复")
    assert labels == ["回答内容不相关", "挂卡/资源缺失"]


def test_issue_labels_whitelist_normalizes_unknown():
    from auto_eval.analysis.operation_statistics import OPERATION_ISSUE_OTHER
    from auto_eval.web.report_payload import _issue_labels

    # 自造/组合/旧标签 → 归一「其他」；同归一标签去重后只有一个「其他」
    labels = _issue_labels(
        "逻辑性/遵从性：组合\n服务闭环：foo\n需求闭环：bar\n旧标签：baz"
    )
    assert labels == [OPERATION_ISSUE_OTHER]
    # 无冒号整行也按标签处理并归一
    assert _issue_labels("整行无冒号的旧标签") == [OPERATION_ISSUE_OTHER]


def test_issue_enum_has_no_blank_and_no_duplicates():
    from auto_eval.analysis.operation_statistics import OPERATION_ISSUE_TYPES

    assert len(OPERATION_ISSUE_TYPES) == len(set(OPERATION_ISSUE_TYPES))
    assert all(label and label == label.strip() for label in OPERATION_ISSUE_TYPES)
    # 需求未闭环是唯一闭环类标签（合并 服务闭环/需求未闭环 等）
    assert "需求未闭环" in OPERATION_ISSUE_TYPES
    assert "服务闭环" not in OPERATION_ISSUE_TYPES
    assert "遵从性" not in OPERATION_ISSUE_TYPES