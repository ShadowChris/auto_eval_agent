"""Explicit zero-based session grouping wins over optional start/end markers."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from fastapi.testclient import TestClient
import pytest

from auto_eval.operation_conversation import normalize_operation_batch, normalize_operation_items
from auto_eval.config import load_config
from auto_eval.web import history, server, tasks
from auto_eval.web.operation_comparison_import import import_operation_comparison_file
from auto_eval.web.parse_input import parse_jsonl


def rows(group="chat-A"):
    return [dict(id=f"{group}-{i}", query=f"第{i}轮问题", answer=f"第{i}轮回答", session_group=group,
                 turn_index=i, video_path="all.mp4", task_start_time=i * 4, task_end_time=(i + 1) * 4,
                 原始标签=i) for i in range(3)]


def parse(source):
    return parse_jsonl("\n".join(json.dumps(row, ensure_ascii=False) for row in source), "operation")


def test_explicit_session_group_sorting_retains_original_rows_and_history_only():
    a, b = rows(), rows("chat-B")
    source = [a[2], b[1], a[0], b[0], a[1], b[2]]
    before = deepcopy(source)
    items, errors = parse(source)
    assert not errors and source == before
    assert [i["id"] for i in items] == [i["id"] for i in source]
    assert [len(i["turns"]) for i in items] == [3, 2, 1, 1, 2, 3]
    assert [i["turn_index"] for i in items] == [3, 2, 1, 1, 2, 3]
    assert [i["source_data"]["turn_index"] for i in items] == [2, 1, 0, 0, 1, 2]
    assert items[4]["turns"][0]["answer"] == a[0]["answer"]
    assert "chat-B" not in json.dumps(items[0], ensure_ascii=False)
    assert a[2]["query"] not in json.dumps(items[4], ensure_ascii=False)
    assert normalize_operation_batch(items) == items
    assert normalize_operation_items(source) == normalize_operation_batch(source)


def test_explicit_pair_wins_even_when_boolean_markers_conflict_or_are_invalid():
    source = rows()
    for row in source:
        row.update(is_start="invalid", is_end=True)
    items, errors = parse(source)
    assert not errors and [len(i["turns"]) for i in items] == [1, 2, 3]
    assert all(i["source_data"]["is_start"] == "invalid" for i in items)


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_empty_pair_falls_back_to_legacy_markers(empty):
    source = rows()
    for i, row in enumerate(source):
        row.update(session_group=empty, turn_index=empty, is_start=i == 0, is_end=i == 2)
    items, errors = parse(source)
    assert not errors and [len(i["turns"]) for i in items] == [1, 2, 3]
    assert all(i["conversation_id"] == source[0]["id"] for i in items)


def test_mixed_group_methods_cannot_bridge_an_explicit_group():
    source = rows()
    legacy = [dict(id="old-0", query="旧首轮", video_path="old.mp4", is_start=True, is_end=False,
                   task_start_time=0, task_end_time=4),
              dict(id="old-1", query="旧末轮", video_path="old.mp4", is_start=False, is_end=True,
                   task_start_time=4, task_end_time=8)]
    items, errors = parse([legacy[0], *source, legacy[1]])
    assert len(items) == 5 and len(errors) == 1
    assert items[0]["input_warnings"] and len(items[0]["turns"]) == 1
    assert items[-1]["input_error"]
    assert all(not i.get("input_error") for i in items[1:4])


@pytest.mark.parametrize("value", [-1, 1.5, True, "bad", float("nan")])
def test_invalid_index_only_fails_its_session(value):
    a, b = rows(), rows("chat-B")
    a[1]["turn_index"] = value
    items, errors = parse([*a, *b])
    assert len(items) == 6 and errors
    assert all(i.get("input_error") for i in items[:3])
    assert all(not i.get("input_error") for i in items[3:])


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_duplicate_round_blocks_only_affected_prefix(duplicate):
    source = rows()
    if duplicate:
        source[2]["turn_index"] = 1
    else:
        source[1]["turn_index"], source[2]["turn_index"] = 2, 3
    items, errors = parse(source)
    assert len(errors) == 2 and not items[0].get("input_error")
    assert all(i.get("input_error") and not i.get("turns") for i in items[1:])


def test_first_turn_must_be_zero_and_does_not_guess_missing_history():
    source = rows()
    for row in source:
        row["turn_index"] += 1
    items, errors = parse(source)
    assert len(errors) == 3 and "turn_index=0" in items[0]["input_error"]


def test_metadata_fields_are_supported_but_explicit_empty_outer_fields_win():
    source = rows()
    for row in source:
        row["metadata"] = {key: row.pop(key) for key in ("session_group", "turn_index")}
    items, errors = parse(source)
    assert not errors and items[0]["session_group"] == "chat-A"
    for i, row in enumerate(source):
        row.update(session_group="", turn_index="", is_start=i == 0, is_end=i == 2)
    items, errors = parse(source)
    assert not errors and items[0]["conversation_id"] == source[0]["id"]


def test_actual_outer_fields_win_over_partial_source_data_without_mutating_input():
    source = rows()
    for row in source:
        row["source_data"] = {"session_group":"stale", "turn_index":99, "标签":"保留"}
    before = deepcopy(source)
    items = normalize_operation_batch(source)
    assert source == before and all(not row.get("input_error") for row in items)
    assert [row["source_data"]["turn_index"] for row in items] == [0, 1, 2]
    assert all(row["session_group"] == "chat-A" and row["source_data"]["标签"] == "保留" for row in items)


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
def test_table_import_without_marker_columns_uses_session_and_preserves_extras(suffix):
    source = [rows()[2], rows()[0], rows()[1]]
    buffer = BytesIO()
    frame = pd.DataFrame(source)
    if suffix == "csv":
        buffer.write(frame.to_csv(index=False).encode())
    else:
        frame.to_excel(buffer, index=False)
    response = TestClient(server.app).post("/api/operation/import-table", files={"file":(f"data.{suffix}",buffer.getvalue())})
    assert response.status_code == 200, response.text
    data = response.json()
    assert not data["errors"]
    assert [int(i["source_data"]["turn_index"]) for i in data["items"]] == [2, 0, 1]
    assert [len(i["turns"]) for i in data["items"]] == [3, 1, 2]


def test_excel_uses_raw_session_fields_and_jsonl_remains_independently_reusable():
    source = [rows()[2], rows()[0], rows()[1]]
    items, errors = parse(source)
    assert not errors
    snapshot = dict(task_id="session", mode="operation", status="done", options={}, items=items,
                    results=[dict(index=i,item_id=row["id"],correctness="ok",issue_types=[],rationale="完成") for i,row in enumerate(items)])
    sheets = history.export_rows(snapshot)
    for name in ("评估结果", "数据集明细"):
        assert [row["session_group"] for row in sheets[name]] == ["chat-A"] * 3
        assert [row["turn_index"] for row in sheets[name]] == [2, 0, 1]
    workbook = load_workbook(BytesIO(history.build_xlsx(snapshot)), data_only=True)
    headers = [cell.value for cell in workbook["评估结果"][1]]
    assert "session_group" in headers and "turn_index" in headers
    assert "评估会话分组" not in headers and "组内轮次" not in headers and "conversation_id" not in headers
    exported = history.jsonl_export_rows(snapshot)
    assert [row["turn_index"] for row in exported] == [2, 0, 1]
    restored, errors = parse(history.jsonl_export_rows(snapshot))
    assert not errors and [len(i["turns"]) for i in restored] == [3, 1, 2]
    assert [i["source_data"]["turn_index"] for i in restored] == [2, 0, 1]
    batch = import_operation_comparison_file("session.xlsx", history.build_xlsx(snapshot))
    assert batch["summary"]["valid_count"] == 3
    assert [row["export"]["session_group"] for row in batch["rows"]] == ["chat-A"] * 3
    assert [row["export"]["turn_index"] for row in batch["rows"]] == [2, 0, 1]


@pytest.mark.asyncio
async def test_batch_api_accepts_session_rows_and_keeps_original_result_indices(monkeypatch, tmp_path):
    source = [rows()[2], rows()[0], rows()[1]]
    cfg = load_config(Path("config"))
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    monkeypatch.setattr(server, "_normalize_eval_options", lambda config, options: (dict(options), config))
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "web_history")
    captured = []

    async def run(task, config):
        captured.extend(task.items)

    monkeypatch.setattr(server, "run_eval", run)
    response = await server.api_eval(server.EvalReq(mode="operation", items=source))
    task = tasks.TASKS[response["task_id"]]
    await task.execution
    assert [row["id"] for row in captured] == [row["id"] for row in source]
    assert [len(row["turns"]) for row in captured] == [3, 1, 2]
    assert all(not row.get("input_error") for row in captured)


def test_single_api_does_not_implicitly_lookup_prior_inputs():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        server._normalize_single_operation_item(rows()[1])
    assert error.value.status_code == 422 and "turn_index=0" in error.value.detail
    single = server._normalize_single_operation_item(dict(id="latest", session_group="chat-A",
                                                         video_path="all.mp4", turns=rows()))
    assert single["id"] == "latest" and len(single["turns"]) == 3
    assert single["source_data"]["turn_index"] == 2
