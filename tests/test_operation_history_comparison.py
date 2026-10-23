from __future__ import annotations

from io import BytesIO

import pytest

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.web import server
from auto_eval.web.history import _OPERATION_USER_RESULT_HEADERS, build_operation_comparison_xlsx
from auto_eval.web.server import OperationHistoryComparisonReq


def _row(
    position: int,
    query: str,
    correctness: str,
    *,
    case_id: str = "",
    data_index: str | int | None = None,
    issue_types: list[str] | None = None,
) -> dict:
    item_id = case_id or (
        str(data_index) if data_index is not None else f"item_{position}"
    )
    return {
        "position": position,
        "index": data_index if data_index is not None else case_id,
        "id": item_id,
        "item_id": item_id,
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


def test_decidable_scope_uses_all_groups_intersection_for_every_metric():
    judgments = [
        ["ok", "nok", "ok", "ok", "ok", "ok"],
        ["nok", "ok", "ok", "no_support", "ok", "ok"],
        ["ok", "ok", "others", "ok", "ok", "ok"],
    ]
    batches = [
        _batch(str(group), str(group), [
            _row(i, f"query {i}", verdict, data_index=i,
                 issue_types=["保留问题" if i < 2 else "排除问题"])
            for i, verdict in enumerate(verdicts)
        ]) for group, verdicts in enumerate(judgments)
    ]
    batches[2]["rows"][4]["result"]["error"] = "timeout"
    batches[2]["rows"].pop()  # 第六条在第三组缺失。
    original = compare_operation_batches(batches, baseline_task_id="0")
    filtered = compare_operation_batches(batches, baseline_task_id="0", scope="common_decidable")
    assert original["all_groups_common_valid_count"] == 4
    assert filtered["common_positions"] == [0, 1]
    assert [g["statistics"]["rate_denominator"] for g in filtered["groups"]] == [2, 2, 2]
    for pair in filtered["pairwise"]:
        assert pair["valid_pair_count"] == 2
        assert pair["baseline_rate_denominator"] == pair["target_rate_denominator"] == 2
        assert [r["issue_type"] for r in pair["issue_type_rows"]] == ["保留问题"]
        assert pair["issue_type_rows"][0]["baseline_rate"] == 1
    assert filtered["pairwise"][0]["to_ok_count"] == 1
    assert filtered["pairwise"][0]["from_ok_count"] == 1
    assert filtered["pairwise"][1]["ok_rate_delta"] == .5
    from auto_eval.analysis.operation_report import build_comparison_report
    report = build_comparison_report(batches, filtered)
    assert all(p["matches"] == [[0, 0], [1, 1]] for p in report["pairs"])


def test_decidable_scope_empty_intersection_has_no_rates():
    batches = [
        _batch("a", "a", [_row(0, "q", "ok")]),
        _batch("b", "b", [_row(0, "q", "others")]),
    ]
    result = compare_operation_batches(batches, baseline_task_id="a", scope="common_decidable")
    assert result["all_groups_common_valid_count"] == 0
    assert result["pairwise"][0]["target_ok_rate"] is None
    assert result["pairwise"][0]["issue_type_rows"] == []
    assert result["pairwise"][0]["ok_rate_change"] == "unavailable"


def test_history_comparison_matches_by_id_and_rejects_query_mismatch() -> None:
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
    assert pair["baseline_rate_denominator"] == 2
    assert pair["target_rate_denominator"] == 2
    assert pair["baseline_ok_rate"] == 0.5
    assert pair["baseline_nok_rate"] == 0.5
    assert pair["target_ok_rate"] == 1.0
    assert pair["target_nok_rate"] == 0.0
    assert pair["ok_rate_delta"] == 0.5
    assert pair["ok_rate_change"] == "improved"
    assert pair["ok_rate_change_label"] == "优化"
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


def test_history_comparison_uses_id_to_align_duplicate_queries() -> None:
    baseline = _batch("baseline", "对照组", [
        _row(0, "打开蓝牙", "ok", data_index="case_1"),
        _row(1, "打开蓝牙", "nok", data_index="case_2"),
    ])
    target = _batch("target", "实验组", [
        _row(0, "打开蓝牙", "ok", data_index="case_2"),
        _row(1, "打开蓝牙", "nok", data_index="case_1"),
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
        include_union_rows=True,
    )

    pair = payload["pairwise"][0]
    assert pair["matched_count"] == 2
    assert pair["to_ok_count"] == 1
    assert pair["from_ok_count"] == 1
    assert [row["match_key"] for row in payload["union_rows"]] == [
        "case_1",
        "case_2",
    ]


def test_history_comparison_excludes_same_id_with_different_query() -> None:
    baseline = _batch("baseline", "对照组", [
        _row(0, "关闭定位", "ok", case_id="same_id", data_index="left_index"),
    ])
    target = _batch("target", "实验组", [
        _row(0, "打开定位", "ok", case_id="same_id", data_index="right_index"),
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
    )

    assert payload["all_groups_common_matched_count"] == 0
    assert payload["alignment_summary"]["query_mismatch"] == 1
    warning = payload["alignment_warnings"][0]
    assert warning["type"] == "query_mismatch"
    assert warning["id"] == "same_id"
    assert warning["baseline_query"] == "关闭定位"
    assert warning["target_query"] == "打开定位"


def test_history_comparison_falls_back_to_unique_query_without_id() -> None:
    baseline = _batch("baseline", "对照组", [{
        **_row(0, "相同 Query", "ok", data_index="same_index"),
        "id": "",
        "item_id": "",
    }])
    target = _batch("target", "实验组", [{
        **_row(0, "相同 Query", "ok", data_index="same_index"),
        "id": "",
        "item_id": "",
    }])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
    )

    assert payload["all_groups_common_matched_count"] == 1
    assert payload["alignment_summary"]["missing_id"] == 2
    assert payload["alignment_summary"]["query_matched"] == 1
    assert payload["pairwise"][0]["alignment_summary"]["id_matched"] == 0


def test_history_comparison_does_not_guess_duplicate_query() -> None:
    baseline = _batch("baseline", "对照组", [
        {**_row(0, "重复 Query", "ok"), "id": "", "item_id": ""},
        {**_row(1, "重复 Query", "nok"), "id": "", "item_id": ""},
    ])
    target = _batch("target", "实验组", [
        {**_row(0, "重复 Query", "ok"), "id": "", "item_id": ""},
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
    )

    assert payload["all_groups_common_matched_count"] == 0
    assert payload["alignment_summary"]["duplicate_query"] == 1
    assert any(
        warning["type"] == "duplicate_query"
        for warning in payload["alignment_warnings"]
    )


def test_history_comparison_does_not_use_query_when_both_ids_differ() -> None:
    baseline = _batch("baseline", "对照组", [
        _row(0, "相同 Query", "ok", case_id="left_id"),
    ])
    target = _batch("target", "实验组", [
        _row(0, "相同 Query", "ok", case_id="right_id"),
    ])

    payload = compare_operation_batches(
        [baseline, target],
        baseline_task_id="baseline",
    )

    assert payload["all_groups_common_matched_count"] == 0
    assert payload["alignment_summary"]["id_conflict"] == 1


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
    assert payload["pairwise"][0]["ok_rate_change"] == "improved"
    assert payload["pairwise"][1]["ok_rate_change"] == "worsened"
    assert [group["group_label"] for group in payload["groups"]] == [
        "对照组",
        "实验组A",
        "实验组B",
    ]
    baseline_counts = {
        row["correctness"]: row["count"]
        for row in payload["groups"][0]["statistics"]["correctness_rows"]
    }
    assert baseline_counts == {"ok": 1, "nok": 1, "no_support": 0}


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
    assert payload["pairwise"][0]["ok_rate_change"] == "close"


def test_history_comparison_has_no_rate_without_ok_or_nok() -> None:
    baseline = _batch("a", "对照组", [
        _row(0, "打开蓝牙", "no_support", case_id="1"),
    ])
    target = _batch("b", "实验组", [
        _row(0, "打开蓝牙", "others", case_id="1"),
    ])

    pair = compare_operation_batches(
        [baseline, target],
        baseline_task_id="a",
    )["pairwise"][0]

    assert pair["valid_pair_count"] == 1
    assert pair["baseline_rate_denominator"] == 0
    assert pair["target_rate_denominator"] == 0
    assert pair["baseline_ok_rate"] is None
    assert pair["target_nok_rate"] is None
    assert pair["ok_rate_change"] == "unavailable"


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
            {"index": 0, "correctness": "ok", "issue_types": ["新增问题"]},
            {"index": 1, "correctness": "ok", "issue_types": []},
        ]),
    }
    snapshots["a"]["items"][0]["source_data"] = {
        "index": "simple_001",
        "session_id": "session-a",
        "attachment_path": "/data/query.png",
        "attachment_url_domain": "https://assets.example/query.png",
        "attachment_url_ip": "http://192.0.2.8/query.png",
        "分享链接": "https://share.example/case-1",
        "video_path": "/data/case-1.mp4",
        "video_url_domain": "https://video.example/case-1.mp4",
        "video_url_ip": "http://192.0.2.9/case-1.mp4",
        "耗时": 12.5,
    }
    snapshots["a"]["results"][0].update({
        "total": 7,
        "rubric": {"操作完成度": 4, "步骤正确性": 3},
        "rubric_reasons": {
            "操作完成度": "状态已完成",
            "步骤正确性": "路径正确",
        },
        "latency_s": 2.5,
    })
    monkeypatch.setattr(server, "get_live_task", lambda _task_id: None)
    monkeypatch.setattr(server, "load_snapshot", lambda task_id: snapshots.get(task_id))
    request = OperationHistoryComparisonReq(
        task_ids=["a", "b"],
        baseline_task_id="a",
    )

    payload = server.api_operation_history_comparison(request)
    assert payload["pairwise"][0]["valid_pair_count"] == 2
    assert payload["pairwise"][0]["target_ok_rate"] == 1.0
    assert payload["ok_rate_close_threshold"] == 0.01
    assert payload["pairwise"][0]["ok_rate_change_label"] == "优化"
    assert [
        row["count_delta"]
        for row in payload["pairwise"][0]["issue_type_rows"]
    ] == [-1, 1]

    response = server.api_operation_history_comparison_export(request)
    assert response.media_type.endswith("spreadsheetml.sheet")
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.load_workbook(BytesIO(response.body), data_only=True)
    assert workbook.sheetnames == [
        "逐题横向对比",
        "对比概览",
        "Issue Type对比",
        "需复核类别对比",
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
    overview_sheet = workbook["对比概览"]
    assert overview_sheet.freeze_panes == "A7"
    assert overview_sheet.sheet_view.showGridLines is False
    assert "A1:R1" in {str(value) for value in overview_sheet.merged_cells.ranges}
    assert overview_sheet["A6"].border.left.style == "thin"
    assert [overview_sheet.cell(row=11, column=column).value for column in range(14, 19)] == [
        "其他→OK",
        "OK→其他",
        "OK净变化",
        "OK率差值",
        "结论",
    ]
    assert overview_sheet["J8"].font.bold is True
    assert overview_sheet["P12"].fill.fgColor.rgb == "FFDCFCE7"
    assert overview_sheet["Q12"].fill.fgColor.rgb == "FFDCFCE7"
    assert overview_sheet["R12"].value == "优化"
    assert overview_sheet["R12"].font.bold is True

    issue_sheet = workbook["Issue Type对比"]
    issue_headers = [cell.value for cell in issue_sheet[1]]
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
        "对照组效果分母",
        "实验组效果分母",
    ]
    assert issue_sheet.auto_filter.ref.startswith("A1:L")
    assert issue_sheet.freeze_panes == "E2"
    assert issue_sheet.sheet_view.showGridLines is False
    assert [issue_sheet.cell(row=row, column=9).value for row in (2, 3)] == [-1, 1]
    assert issue_sheet["I2"].fill.fgColor.rgb == "FFDCFCE7"
    assert issue_sheet["I3"].fill.fgColor.rgb == "FFFEE2E2"
    assert issue_sheet["A2"].border.left.style == "thin"
    assert "pp" in issue_sheet["J2"].number_format

    detail_sheet = workbook["逐题横向对比"]
    detail_group_headers = [cell.value for cell in detail_sheet[1]]
    detail_headers = [cell.value for cell in detail_sheet[2]]
    assert "对照组：对照组" in detail_group_headers
    assert "实验组A：实验组" in detail_group_headers
    assert "query" in detail_headers
    assert detail_headers.count("index") == 2
    assert "correctness" in detail_headers
    assert "所有组共有" in detail_headers
    field_count = len(_OPERATION_USER_RESULT_HEADERS)
    assert tuple(detail_headers[3:3 + field_count]) == _OPERATION_USER_RESULT_HEADERS
    assert tuple(detail_headers[3 + field_count:3 + 2 * field_count]) == (
        _OPERATION_USER_RESULT_HEADERS
    )
    baseline_values = dict(zip(
        _OPERATION_USER_RESULT_HEADERS,
        [cell.value for cell in detail_sheet[3]][3:3 + field_count],
    ))
    assert "是否挂卡" in baseline_values
    assert "card_presence" not in baseline_values
    assert baseline_values["attachment_path"] == "/data/query.png"
    assert baseline_values["维度_操作完成度"] == 4
    assert baseline_values["理由_步骤正确性"] == "路径正确"
    assert baseline_values["video_url_domain"] == "https://video.example/case-1.mp4"
    assert baseline_values["录屏时长"] == 12.5
    merged_ranges = {str(value) for value in detail_sheet.merged_cells.ranges}
    assert "A1:C1" in merged_ranges
    assert "D1:AI1" in merged_ranges
    assert "AJ1:BO1" in merged_ranges
    assert detail_sheet.freeze_panes == "D3"
    assert detail_sheet.auto_filter.ref.startswith("A2:BO")
    assert detail_sheet["A3"].border.left.style == "thin"
    assert detail_sheet["D3"].alignment.wrap_text is False


def test_comparison_excel_uses_distinct_headers_for_all_five_groups() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    batches = [
        _batch(str(index), f"group-{index}", [
            _row(0, "打开蓝牙", "ok", case_id="same"),
        ])
        for index in range(5)
    ]
    payload = compare_operation_batches(batches, baseline_task_id="0", include_union_rows=True)
    workbook = openpyxl.load_workbook(BytesIO(build_operation_comparison_xlsx(payload)))
    assert workbook.active.title == "逐题横向对比"
    sheet = workbook.worksheets[0]
    field_count = len(_OPERATION_USER_RESULT_HEADERS)
    title_colors, field_colors = set(), set()
    for index in range(5):
        first_column = 4 + index * field_count
        title_cell = sheet.cell(1, first_column)
        title_colors.add(title_cell.fill.fgColor.rgb)
        fields = [sheet.cell(2, column) for column in range(first_column, first_column + field_count)]
        group_colors = {cell.fill.fgColor.rgb for cell in fields}
        assert len(group_colors) == 1
        field_colors.update(group_colors)
        assert title_cell.fill.fgColor.rgb not in group_colors
        assert title_cell.font.bold is True
        assert all(cell.font.bold and not cell.alignment.wrap_text for cell in fields)
        assert sheet.cell(3, first_column).fill.fgColor.rgb == "FFFFFFFF"
    assert len(title_colors) == len(field_colors) == 5
    assert sheet.freeze_panes == "D3"


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
    assert "只看劣化" in html
    assert "问题减少（优化）" in html
    assert "comparison-best-rate" in html
    assert "comparisonPairChangeLabel(pair)" in html
    assert "OK率差值" in html
    assert "ok_rate_close_threshold" in js
    assert "comparisonPairChangeClass" in js
    assert "问题增加（劣化）" in html
    assert "频次差值（实验组-对照组）" in html
    assert "comparisonIssueRows" in js
    assert 'pair._issue_sort_by = "count_delta"' in js
    assert 'pair._issue_sort_direction = "asc"' in js
    assert "comparisonIssueDeltaStyle" in js
    assert "comparisonIssueDeltaText" in js
    assert 'item?.mode === "operation"' in js
    assert 'item?.operation_layout !== "multi_group"' in js
    assert '["completed", "partial_completed", "done"].includes(item?.status)' in js
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
