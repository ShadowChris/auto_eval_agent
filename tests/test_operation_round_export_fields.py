"""多轮导出保留真实边界，不从历史前缀、失败子集或会话长度猜测标记。"""
from copy import deepcopy
from io import BytesIO
import json

from openpyxl import load_workbook
import pytest

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.web import history
from auto_eval.web.operation_comparison_import import import_operation_comparison_file
from auto_eval.web.operation_export_fields import operation_round_export_fields
from auto_eval.web.parse_input import parse_jsonl


COLUMNS = ("conversation_id", "turn_index", "is_start", "is_end")
DISPLAY_COLUMNS = ("session_group", "turn_index", "is_start", "is_end")


def _snapshot(task_id="rounds", *, partial_source=False):
    records = [
        dict(id="r1", query="设置闹钟", video_path="data/all.mp4", task_start_time=0,
             task_end_time=4, is_start="TRUE", is_end=0),
        dict(id="r2", query="明早七点", video_path="", task_start_time=4,
             task_end_time=8, is_start=0, is_end="TRUE"),
    ]
    for record in records:
        record.update(session_id="business", session_group="original-group")
        if partial_source:
            record["source_data"] = dict(record)
            record["source_data"].pop("is_start")
            record["source_data"].pop("is_end")
    items, errors = parse_jsonl("\n".join(json.dumps(row) for row in records), "operation")
    assert not errors
    return dict(task_id=task_id, mode="operation", status="done", options={}, items=items,
                results=[dict(index=i, correctness="ok", rationale="完成") for i in range(2)])


@pytest.mark.parametrize("partial_source", [False, True])
def test_single_excel_and_details_keep_markers_and_grouping(partial_source):
    snapshot = _snapshot(partial_source=partial_source)
    before = deepcopy(snapshot)
    sheets = history.export_rows(snapshot)
    expected = [["r1", 1, "TRUE", 0], ["r1", 2, 0, "TRUE"]]
    for name in ("评估结果", "数据集明细"):
        keys = DISPLAY_COLUMNS if name == "评估结果" else COLUMNS
        display_expected = [["original-group", "", "TRUE", 0], ["original-group", "", 0, "TRUE"]]
        assert [[row[key] for key in keys] for row in sheets[name]] == (display_expected if name == "评估结果" else expected)
    assert [row["session_group"] for row in sheets["数据集明细"]] == ["original-group"] * 2
    workbook = load_workbook(BytesIO(history.build_xlsx(snapshot)), data_only=True)
    for name in ("评估结果", "数据集明细"):
        sheet = workbook[name]
        headers = [cell.value for cell in sheet[1]]
        keys = DISPLAY_COLUMNS if name == "评估结果" else COLUMNS
        columns = [headers.index(key) + 1 for key in keys]
        if name == "评估结果":
            assert "conversation_id" not in headers and "组内轮次" not in headers
        workbook_expected = display_expected
        assert [[sheet.cell(row, col).value for col in columns] for row in (2, 3)] == (workbook_expected if name == "评估结果" else expected)
    headers = [cell.value for cell in workbook["评估结果"][1]]
    start = headers.index("session_id") + 1
    assert headers[start:start + 4] == list(DISPLAY_COLUMNS)
    assert snapshot == before


@pytest.mark.parametrize("uploaded", [False, True])
def test_comparison_shares_same_round_fields(uploaded):
    baseline = history.operation_comparison_batch(_snapshot(), include_source_data=True)
    experiment = _snapshot("experiment")
    target = (import_operation_comparison_file("rounds.xlsx", history.build_xlsx(experiment))
              if uploaded else history.operation_comparison_batch(experiment, include_source_data=True))
    payload = compare_operation_batches([baseline, target], baseline_task_id="rounds", include_union_rows=True)
    workbook = load_workbook(BytesIO(history.build_operation_comparison_xlsx(payload)), data_only=True)
    sheet = workbook["逐题横向对比"]
    headers = [cell.value for cell in sheet[2]]
    for key, expected in zip(DISPLAY_COLUMNS, ["original-group", "", "TRUE", 0]):
        columns = [i + 1 for i, value in enumerate(headers) if value == key]
        assert len(columns) == 2
        assert [sheet.cell(3, col).value for col in columns] == [expected, expected]
    assert "conversation_id" not in headers and "组内轮次" not in headers


def test_old_runtime_markers_are_not_hidden_by_partial_source_data():
    snapshot = dict(mode="operation", items=[dict(id="old", query="打开蓝牙", turn_index=0,
                    is_start=False, is_end=0, session_group="legacy", source_data={"标签": "保留"})])
    sheets = history.export_rows(snapshot)
    for name in ("评估结果", "数据集明细"):
        row = sheets[name][0]
        assert row["turn_index"] == 0
        assert row["is_start"] is False
        assert row["is_end"] == 0
    assert sheets["数据集明细"][0]["标签"] == "保留"


def test_current_round_fallback_never_uses_a_prior_round_or_guesses_end():
    item = dict(evaluation_scope="latest_turn", conversation_id="c", turn_index=2, turn_count=2,
                turns=[dict(source_data=dict(is_start=True, is_end=False)), dict(source_data={})])
    fields = operation_round_export_fields(item, {})
    assert fields == dict(conversation_id="c", session_group="", turn_index="", is_start="", is_end="")
    item["turns"][-1]["source_data"] = dict(metadata=dict(is_start=0, is_end=False))
    fields = operation_round_export_fields(item, {})
    assert fields["is_start"] == 0 and fields["is_end"] is False
    fields = operation_round_export_fields(item, dict(is_start="FALSE", is_end="invalid"))
    assert fields["is_start"] == "FALSE" and fields["is_end"] == "invalid"
    assert operation_round_export_fields({}, {}) == dict.fromkeys((*COLUMNS, "session_group"), "")


def test_failed_pending_and_selected_later_round_keep_original_boundaries():
    snapshot = _snapshot()
    snapshot["results"] = [dict(index=0, error="timeout")]
    sheets = history.export_rows(snapshot)
    assert [row["correctness"] for row in sheets["评估结果"]] == ["", ""]
    assert [row["is_end"] for row in sheets["评估结果"]] == [0, "TRUE"]
    snapshot["items"] = snapshot["items"][1:]
    snapshot["results"] = []
    row = history.export_rows(snapshot)["评估结果"][0]
    assert row["turn_index"] == "" and row["is_start"] == 0 and row["is_end"] == "TRUE"


def test_single_turn_without_saved_markers_leaves_columns_empty():
    snapshot = dict(mode="operation", items=[dict(id="s", query="打开蓝牙", source_data={"id": "s"})])
    for name in ("评估结果", "数据集明细"):
        row = history.export_rows(snapshot)[name][0]
        keys = DISPLAY_COLUMNS if name == "评估结果" else COLUMNS
        assert [row[key] for key in keys] == [""] * 4


@pytest.mark.parametrize("chinese", [False, True])
def test_uploaded_result_only_recognizes_both_old_and_display_headers(chinese):
    record = dict(id="a", query="打开蓝牙", correctness="ok", issue_types=[], rationale="完成")
    group_key, turn_key = ("评估会话分组", "组内轮次") if chinese else COLUMNS[:2]
    record.update({group_key: "group-a", turn_key: 0})
    batch = import_operation_comparison_file("results.jsonl", json.dumps(record).encode())
    exported = batch["rows"][0]["export"]
    assert exported["conversation_id"] == "group-a"
    assert exported["turn_index"] == 0
    assert "评估会话分组" not in exported and "组内轮次" not in exported
