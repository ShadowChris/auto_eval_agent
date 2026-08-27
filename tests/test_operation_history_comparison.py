from __future__ import annotations

from io import BytesIO

import pytest

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.web import server
from auto_eval.web.server import OperationHistoryComparisonReq


def _row(
    position: int,
    query: str,
    correctness: str,
    *,
    case_id: str = "",
    issue_types: list[str] | None = None,
) -> dict:
    return {
        "position": position,
        "item_id": f"item_{position}",
        "case_id": case_id,
        "query": query,
        "result": {
            "correctness": correctness,
            "issue_types": issue_types or [],
            "rationale": f"{query} 的理由",
        },
    }


def _batch(task_id: str, name: str, rows: list[dict]) -> dict:
    return {
        "task_id": task_id,
        "dataset_name": f"{name}.jsonl",
        "rows": rows,
    }


def test_history_comparison_matches_by_case_id_then_unique_query() -> None:
    baseline = _batch("baseline", "对照组", [
        _row(0, "打开蓝牙", "ok", case_id="c1"),
        _row(1, "关闭定位", "nok", case_id="c2", issue_types=["应执行目标未执行"]),
        _row(2, "打开深色模式", "no_support", issue_types=["缺少必要外部条件"]),
        _row(3, "设置字号", "others", case_id="c4", issue_types=["未预期场景"]),
    ])
    target = _batch("target", "实验组", [
        _row(0, "打开蓝牙", "ok", case_id="c1"),
        _row(1, "关闭定位", "ok", case_id="c2"),
        _row(2, "打开深色模式", "no_support", issue_types=["缺少必要外部条件"]),
        _row(3, "设置字体为特大", "ok", case_id="c4"),
        _row(4, "额外任务", "ok", case_id="extra"),
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
    )

    assert payload["all_groups_common_matched_count"] == 3
    assert payload["all_groups_common_valid_count"] == 3
    pair = payload["pairwise"][0]
    assert pair["matched_count"] == 3
    assert pair["valid_pair_count"] == 3
    assert pair["baseline_ok_rate"] == 0.3333
    assert pair["target_ok_rate"] == 0.6667
    assert pair["ok_rate_delta"] == 0.3333
    assert pair["to_ok_count"] == 1
    assert pair["from_ok_count"] == 0
    assert pair["net_ok_change"] == 1
    assert [group["group_label"] for group in payload["groups"]] == [
        "对照组",
        "实验组A",
    ]
    assert [group["original_count"] for group in payload["groups"]] == [4, 5]
    assert all(group["common_valid_count"] == 3 for group in payload["groups"])
    issue = next(
        row for row in pair["issue_type_rows"]
        if row["issue_type"] == "应执行目标未执行"
    )
    assert issue["baseline_count"] == 1
    assert issue["target_count"] == 0
    assert issue["count_delta"] == -1


def test_history_comparison_uses_one_baseline_for_multiple_batches() -> None:
    baseline = _batch("a", "A", [
        _row(0, "q1", "ok", case_id="1"),
        _row(1, "q2", "nok", case_id="2"),
    ])
    group_b = _batch("b", "B", [
        _row(0, "q1", "ok", case_id="1"),
        _row(1, "q2", "ok", case_id="2"),
    ])
    group_c = _batch("c", "C", [
        _row(0, "q1", "nok", case_id="1"),
        _row(1, "q2", "nok", case_id="2"),
    ])

    payload = compare_operation_batches(
        [baseline, group_b, group_c],
        baseline_task_id="a",
    )

    assert payload["group_count"] == 3
    assert payload["all_groups_common_valid_count"] == 2
    assert [pair["target_task_id"] for pair in payload["pairwise"]] == ["b", "c"]
    assert payload["pairwise"][0]["net_ok_change"] == 1
    assert payload["pairwise"][1]["net_ok_change"] == -1
    assert [group["group_label"] for group in payload["groups"]] == [
        "对照组",
        "实验组A",
        "实验组B",
    ]
    baseline_counts = {
        row["correctness"]: row["count"]
        for row in payload["groups"][0]["statistics"]["correctness_rows"]
    }
    assert baseline_counts == {"ok": 1, "nok": 1, "no_support": 0, "others": 0}


def test_history_comparison_preserves_dataset_name_with_version_dot() -> None:
    baseline = _batch("a", "rom7.0_众测980_对照组", [
        _row(0, "q1", "ok", case_id="1"),
    ])
    target = _batch("b", "rom7.0_众测980_实验组", [
        _row(0, "q1", "ok", case_id="1"),
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="a",
    )

    assert [group["group_name"] for group in payload["groups"]] == [
        "rom7.0_众测980_对照组.jsonl",
        "rom7.0_众测980_实验组.jsonl",
    ]


def test_history_comparison_uses_all_group_valid_intersection_and_union_export() -> None:
    baseline = _batch("a", "A", [
        _row(0, "q1", "ok", case_id="1"),
        _row(1, "q2", "nok", case_id="2"),
    ])
    group_b = _batch("b", "B", [
        _row(0, "q1", "ok", case_id="1"),
        _row(1, "q2", "ok", case_id="2"),
        _row(2, "q3", "ok", case_id="3"),
    ])
    group_c = _batch("c", "C", [
        _row(0, "q1", "nok", case_id="1"),
        _row(1, "q3", "others", case_id="3"),
    ])

    payload = compare_operation_batches(
        [baseline, group_b, group_c],
        baseline_task_id="a",
        include_union_rows=True,
    )

    assert payload["all_groups_common_matched_count"] == 1
    assert payload["all_groups_common_valid_count"] == 1
    assert all(group["common_valid_count"] == 1 for group in payload["groups"])
    assert [pair["valid_pair_count"] for pair in payload["pairwise"]] == [2, 1]
    union = payload["union_rows"]
    assert len(union) == 3
    assert [row["present_group_count"] for row in union] == [3, 2, 2]
    assert [row["all_groups_present"] for row in union] == [True, False, False]


def _snapshot(task_id: str, name: str, results: list[dict]) -> dict:
    return {
        "task_id": task_id,
        "dataset_name": f"{name}.jsonl",
        "mode": "operation",
        "status": "done",
        "options": {},
        "items": [
            {"id": "i1", "case_id": "c1", "query": "打开蓝牙"},
            {"id": "i2", "case_id": "c2", "query": "关闭定位"},
        ],
        "results": results,
        "summary": {},
    }


def test_history_comparison_api_and_xlsx_export(monkeypatch) -> None:
    snapshots = {
        "a": _snapshot("a", "对照组", [
            {"index": 0, "correctness": "ok", "issue_types": []},
            {"index": 1, "correctness": "nok", "issue_types": ["应执行目标未执行"]},
        ]),
        "b": _snapshot("b", "实验组", [
            {"index": 0, "correctness": "ok", "issue_types": []},
            {"index": 1, "correctness": "ok", "issue_types": []},
        ]),
    }
    monkeypatch.setattr(server, "get_live_task", lambda _task_id: None)
    monkeypatch.setattr(server, "load_snapshot", lambda task_id: snapshots.get(task_id))
    request = OperationHistoryComparisonReq(
        task_ids=["a", "b"],
        baseline_task_id="a",
    )

    payload = server.api_operation_history_comparison(request)
    assert payload["pairwise"][0]["valid_pair_count"] == 2
    assert payload["pairwise"][0]["target_ok_rate"] == 1.0

    response = server.api_operation_history_comparison_export(request)
    assert response.media_type.endswith("spreadsheetml.sheet")
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.load_workbook(BytesIO(response.body), data_only=True)
    assert workbook.sheetnames == [
        "对比概览",
        "Issue Type对比",
        "逐题横向对比",
    ]
    overview_values = list(workbook["对比概览"].values)
    assert overview_values[5][:5] == (
        "组别",
        "数据集",
        "task_id",
        "原始数据量",
        "共有有效数据量",
    )
    assert overview_values[6][0] == "对照组"
    issue_headers = [cell.value for cell in workbook["Issue Type对比"][1]]
    assert issue_headers == [
        "对比关系",
        "实验组",
        "共同有效Case",
        "Issue Type",
        "对照组频次",
        "对照组占比",
        "实验组频次",
        "实验组占比",
        "频次差值",
        "占比差值",
    ]
    assert workbook["Issue Type对比"].auto_filter.ref.startswith("A1:J")
    detail_headers = [cell.value for cell in workbook["逐题横向对比"][1]]
    assert "对照组_query" in detail_headers
    assert "实验组A_correctness" in detail_headers
    assert "所有组共有" in detail_headers


def test_history_comparison_api_rejects_unfinished_or_multi_group(monkeypatch) -> None:
    unfinished = _snapshot("a", "A", [])
    unfinished["status"] = "running"
    multi = _snapshot("b", "B", [])
    multi["options"] = {"operation_layout": "multi_group"}
    snapshots = {"a": unfinished, "b": multi}
    monkeypatch.setattr(server, "get_live_task", lambda _task_id: None)
    monkeypatch.setattr(server, "load_snapshot", lambda task_id: snapshots.get(task_id))

    request = OperationHistoryComparisonReq(
        task_ids=["a", "b"],
        baseline_task_id="a",
    )
    with pytest.raises(server.HTTPException) as raised:
        server.api_operation_history_comparison(request)
    assert raised.value.status_code == 409
    assert "只能对比已完成任务" in str(raised.value.detail)


def test_history_comparison_ui_links_to_comparison_workspace() -> None:
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "已选 {{ comparisonSelectedCount }}/5 个已完成任务类批次" in html
    assert "生成对比" in html
    assert "导出对比 XLSX" in html
    assert "其他→OK" in html
    assert "OK→其他" in html
    assert "筛选 Issue Type" in html
    assert "实验组频次" in html
    assert "comparisonIssueRows" in js
    assert 'item?.mode === "operation"' in js
    assert 'item?.operation_layout !== "multi_group"' in js
    assert 'item?.status === "done"' in js
    assert "加入对比分析" in html
    assert "taskModule==='comparison'" in html
    assert "第一组为对照组" in html
    assert "moveComparisonSource(index,-1)" in html
    assert "moveComparisonSource(index,1)" in html
    assert "function moveComparisonSource" in js
    assert "beginComparisonSourceNameEdit" in js
    assert "saveComparisonSourceName" in js
    assert "task_id: {{ source.task_id || source.source_id }}" in html
    assert 'fetch("/api/operation/comparison/analyze"' in js
