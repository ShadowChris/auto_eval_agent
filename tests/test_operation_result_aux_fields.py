"""Internal conversation metadata stays in details/JSONL, not result sheets."""
from copy import deepcopy
from io import BytesIO
import json

from openpyxl import load_workbook
import pytest

from auto_eval.operation_conversation import group_operation_items, normalize_operation_items
from auto_eval.web import history
from auto_eval.web.operation_comparison_import import (
    _restore_legacy_conversation_fields,
    import_operation_comparison_file,
)
from auto_eval.web.operation_export_fields import (
    OPERATION_INTERNAL_ROUND_FIELDS,
    OPERATION_USER_RESULT_HEADERS,
    operation_user_result_display_row,
)
from auto_eval.web.parse_input import parse_jsonl


def snapshot(*, legacy=False, state="done"):
    source = [
        dict(id="r1", query="设一个闹钟", answer="请问几点？", video_path="data/all.mp4",
             is_start="TRUE", is_end="FALSE", task_start_time=0, task_end_time=4, 标签="首轮"),
        dict(id="r2", query="明早七点", answer="已设置", video_path="",
             is_start="FALSE", is_end="TRUE", task_start_time=4, task_end_time=8, 标签="续轮"),
        dict(id="s1", query="关闭蓝牙", answer="已关闭", video_path="data/single.mp4", 标签="单轮"),
    ]
    items = group_operation_items(source) if legacy else normalize_operation_items(source)
    results = [dict(index=index, item_id=row["id"], correctness="ok", total=5,
                    issue_types=[], need_review_cate=[], rationale="本轮完成", latency_s=1)
               for index, row in enumerate(items)]
    if state == "failed":
        results[0] = dict(index=0, item_id="r1", error="timeout")
    elif state == "pending":
        results = []
    return dict(task_id="aux-demo", mode="operation", status="running" if state == "pending" else "done",
                options={}, items=items, results=results)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("state", ["done", "failed", "pending"])
def test_only_internal_aux_fields_are_hidden_and_other_round_fields_remain(legacy, state):
    data = snapshot(legacy=legacy, state=state)
    original = deepcopy(data)
    jsonl = history.rows_to_jsonl(history.jsonl_export_rows(data))
    stats = history.operation_statistics_payload(data)
    sheets = history.export_rows(data)
    workbook = load_workbook(BytesIO(history.build_xlsx(data)), data_only=True)
    for name in ("评估结果", "逐题结果"):
        assert len(sheets[name]) == 3
        assert all(not set(row).intersection(OPERATION_INTERNAL_ROUND_FIELDS) for row in sheets[name])
        headers = [cell.value for cell in workbook[name][1]]
        assert not set(headers).intersection(OPERATION_INTERNAL_ROUND_FIELDS)
    assert list(sheets["评估结果"][0]) == list(OPERATION_USER_RESULT_HEADERS)
    assert all(key in sheets["评估结果"][0] for key in ("session_group", "turn_index", "is_start", "is_end"))
    details = sheets["数据集明细"]
    assert [row["标签"] for row in details] == ["首轮", "续轮", "单轮"]
    assert [row["case_id"] for row in details[:2]] == ["r1", "r1"]
    assert [row["turn_count"] for row in details[:2]] == [2, 2]
    if legacy:
        assert [row["is_result_row"] for row in details] == [True, False, True]
        assert sheets["评估结果"][1]["correctness"] == sheets["逐题结果"][1]["error"] == ""
    else:
        assert [row["evaluation_scope"] for row in details[:2]] == ["latest_turn"] * 2
        assert [row["conversation_id"] for row in details[:2]] == ["r1"] * 2
    assert history.rows_to_jsonl(history.jsonl_export_rows(data)) == jsonl
    assert history.operation_statistics_payload(data) == stats
    assert data == original


@pytest.mark.parametrize("legacy,expected", [(False, 3), (True, 2)])
def test_xlsx_comparison_reimport_keeps_previous_count_and_scope(legacy, expected):
    data = snapshot(legacy=legacy)
    batch = import_operation_comparison_file("results.xlsx", history.build_xlsx(data))
    assert batch["summary"]["valid_count"] == expected
    assert batch["summary"]["invalid_count"] == 0
    assert batch["rows"][0]["result"]["correctness"] == "ok"
    assert batch["rows"][0]["source_data"]["turn_count"] == 2


def test_legacy_marker_recovery_matches_identity_not_detail_order_or_scores():
    records = [dict(id="a", query="第一题", correctness="nok", rationale="新评估"),
               dict(id="b", query="第二题")]
    details = [dict(id="b", query="第二题", case_id="group", turn_count=2, turn_index=2, is_result_row=False),
               dict(id="a", query="第一题", case_id="group", turn_count=2, turn_index=1, is_result_row=True,
                    correctness="ok", rationale="旧标签")]
    before = deepcopy(records)
    restored = _restore_legacy_conversation_fields(records, details)
    assert restored[0]["is_result_row"] is True and restored[1]["is_result_row"] is False
    assert restored[0]["turn_index"] == 1 and restored[1]["turn_index"] == 2
    assert restored[0]["correctness"] == "nok" and restored[0]["rationale"] == "新评估"
    assert records == before
    records[0]["case_id"] = "existing"
    assert _restore_legacy_conversation_fields(records, details)[0]["case_id"] == "existing"


def test_legacy_marker_recovery_does_not_guess_ambiguous_ids():
    records = [dict(id="a", query="第一题")]
    details = [dict(id="a", query="第一题", is_result_row=True),
               dict(id="a", query="第一题", is_result_row=False)]
    assert _restore_legacy_conversation_fields(records, details) == records


def test_display_projection_drops_aux_without_mutating_input():
    raw = dict(case_id="business", turn_count=3, evaluation_scope="latest_turn", is_result_row=True,
               conversation_id="internal", session_group="chat", turn_index=2, is_start=False, is_end=True)
    projected = operation_user_result_display_row(raw)
    assert not set(projected).intersection(OPERATION_INTERNAL_ROUND_FIELDS)
    assert projected["session_group"] == "chat" and projected["turn_index"] == 2
    assert projected["is_start"] is False and projected["is_end"] is True
    assert raw["case_id"] == "business"


def test_failure_subset_still_selects_only_current_round_with_prefix(tmp_path):
    from scripts.excel_eval_failed_subset import extract_failed_subset

    data = snapshot()
    data["results"][1] = dict(index=1, item_id="r2", error="timeout")
    path = tmp_path / "results.xlsx"
    path.write_bytes(history.build_xlsx(data))
    source = tmp_path / "source.jsonl"
    source.write_text("\n".join(json.dumps(item.get("source_data") or item) for item in data["items"]))
    subset = extract_failed_subset(path, source)
    assert subset.selected_rows == 1
    selected, errors = parse_jsonl(subset.output_path.read_text(), "operation")
    assert not errors and selected[0]["id"] == "r2" and len(selected[0]["turns"]) == 2
