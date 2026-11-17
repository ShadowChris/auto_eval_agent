"""Batch input failures preserve source rows, without weakening time/media checks."""
import copy
import io
import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from auto_eval.config import load_config
from auto_eval.operation_conversation import normalize_operation_batch
from auto_eval.web import history, runner, server, tasks
from auto_eval.web.operation_media import _operation_timing, prepare_operation_query_images
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.tasks import Task


def rows():
    return [dict(id=f"r{index + 1}", query=f"轮次{index + 1}", answer="回答",
                 video_path="data/all.mp4" if index == 0 else "",
                 task_start_time=index * 4, task_end_time=(index + 1) * 4,
                 is_start="TRUE" if index == 0 else "FALSE",
                 is_end="TRUE" if index == 2 else "FALSE", 自定义=index)
            for index in range(3)]


def parse(source):
    return parse_jsonl("\n".join(json.dumps(row, ensure_ascii=False) for row in source), "operation")


def test_video_can_come_from_any_round_without_changing_original_fields():
    source = rows()
    source[0]["video_path"], source[2]["video_path"] = "", "data/all.mp4"
    original = copy.deepcopy(source)
    items, errors = parse(source)
    assert not errors and source == original
    assert [i["video_path"] for i in items] == ["data/all.mp4"] * 3
    assert items[0]["source_data"]["video_path"] == ""
    assert all("复用" in i["input_warnings"][0] for i in items)
    assert normalize_operation_batch(items) == items


@pytest.mark.parametrize("change", [
    {"task_start_time": None}, {"task_end_time": "bad"}, {"task_end_time": float("inf")},
    {"task_start_time": -1}, {"task_end_time": 4}, {"task_start_time": 3},
    {"query_images": [None]}, {"query": ""}, {"context": []}, {"answer": 1},
])
def test_invalid_round_only_blocks_itself_and_dependent_prefixes(change):
    source = rows()
    source[1].update(change)
    source.append(dict(id="single", query="独立任务", video_path="data/single.mp4"))
    items, errors = parse(source)
    assert len(items) == 4 and len(errors) == 2
    assert not items[0].get("input_error") and len(items[0]["turns"]) == 1
    assert items[1]["input_error"] and "依赖第 2 轮" in items[2]["input_error"]
    assert not items[3].get("input_error")
    assert [i["id"] for i in items] == ["r1", "r2", "r3", "single"]
    assert all(not item.get("turns") for item in items[1:3])
    assert [i["source_data"]["自定义"] for i in items[:3]] == [0, 1, 2]
    assert normalize_operation_batch(items) == items


@pytest.mark.parametrize("conflict", [False, True])
def test_missing_or_conflicting_recording_only_fails_one_conversation(conflict):
    source = rows()
    if conflict:
        source[1]["video_path"] = "different.mp4"
    else:
        source[0]["video_path"] = "__missing_video__"
    source.append(dict(id="other", query="其他", video_path="other.mp4"))
    items, errors = parse(source)
    assert len(errors) == 3 and len(items) == 4
    assert all(i.get("input_error") for i in items[:3])
    assert not items[-1].get("input_error")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_time_does_not_break_parse_response_or_jsonl_export(value):
    source = rows()
    source[1]["task_end_time"] = value
    response = TestClient(server.app).post("/api/parse", json={
        "mode": "operation", "jsonl": "\n".join(json.dumps(row) for row in source),
    })
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 3 and len(data["errors"]) == 2
    assert not data["items"][0].get("input_error")
    exported = history.jsonl_export_rows({"mode": "operation", "items": data["items"], "results": []})
    history.rows_to_jsonl(exported)


@pytest.mark.parametrize("eof", [False, True])
def test_missing_end_is_closed_at_explicit_start_or_eof_with_warning(eof):
    source = rows()
    source[-1]["is_end"] = "FALSE"
    if not eof:
        source += [dict(id="next", query="新任务", video_path="next.mp4", is_start="TRUE", is_end="TRUE")]
    items, errors = parse(source)
    assert not errors and len(items) == len(source)
    assert all(i.get("input_warnings") for i in items[:3])
    assert len(items[2]["turns"]) == 3
    if not eof:
        assert not items[-1].get("turns")


def test_damaged_json_is_not_dropped_and_next_explicit_start_recovers():
    source = rows()
    text = json.dumps(source[0]) + '\n{"damaged":\n' + json.dumps(source[2])
    text += '\n' + json.dumps(dict(id="recover", query="新会话", video_path="next.mp4", is_start=True, is_end=True))
    items, errors = parse_jsonl(text, "operation")
    assert len(items) == 4 and len(errors) == 2
    assert not items[0].get("input_error")
    assert "JSON 错误" in items[1]["input_error"]
    assert "依赖第 2 轮" in items[2]["input_error"]
    assert not items[3].get("input_error")
    assert [i["source_line"] for i in items] == [1, 2, 3, 4]


def test_explicit_start_with_bad_end_still_separates_previous_conversation():
    source = rows()
    source[1].update(is_start="TRUE", is_end="bad", video_path="next.mp4")
    items, errors = parse(source)
    assert len(items) == 3 and len(errors) == 2
    assert not items[0].get("input_error") and items[0]["input_warnings"]
    assert items[1]["conversation_id"] != items[0]["conversation_id"]
    assert "依赖第 1 轮" in items[2]["input_error"]


def test_orphan_and_bad_internal_prefix_do_not_block_later_cases():
    source = rows()
    source[0]["is_start"] = False
    source += [dict(id="bad", query="坏前缀", video_path="bad.mp4", turns=[None]),
               dict(id="good", query="独立任务", video_path="good.mp4")]
    items, errors = parse(source)
    assert len(items) == 5 and len(errors) == 4
    assert not items[-1].get("input_error")
    prefix = parse(rows())[0][0]
    prefix["turn_index"] = 3
    isolated = normalize_operation_batch([prefix, source[-1]])
    assert isolated[0]["input_error"] and not isolated[1].get("input_error")


def test_duplicate_ids_never_overwrite_rows_or_reject_unrelated_inputs():
    source = rows()
    source[1]["id"] = source[0]["id"]
    items, errors = parse(source)
    assert len(items) == 3 and len(errors) == 2
    assert len({i["id"] for i in items}) == 3
    assert [i["source_data"]["id"] for i in items] == ["r1", "r1", "r3"]


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
@pytest.mark.parametrize("image_cell", ["[123]", "[null]", '[""]'])
def test_table_import_retains_invalid_time_images_and_duplicate_ids(suffix, image_cell):
    source = rows()
    source[1].update(task_end_time=3, query_images=image_cell, id="r1")
    frame = pd.DataFrame(source)
    buffer = io.BytesIO()
    if suffix == "csv":
        buffer.write(frame.to_csv(index=False).encode())
    else:
        frame.to_excel(buffer, index=False)
    response = TestClient(server.app).post("/api/operation/import-table",
        files={"file": (f"test.{suffix}", buffer.getvalue())})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["items"]) == 3 and data["errors"]
    assert all(i.get("input_error") for i in data["items"])
    assert "invalid_query_images" in data["items"][2]["input_error"]


def test_actual_media_checks_remain_strict_and_do_not_affect_earlier_prefix(tmp_path):
    source = rows()
    source[1]["query_images"] = ["missing.jpg"]
    items, errors = parse(source)
    assert not errors  # File resources are checked only on execution.
    prepare_operation_query_images(items[0], base_dir=tmp_path)
    for item in items[1:]:
        with pytest.raises(ValueError, match="图片不存在"):
            prepare_operation_query_images(item, base_dir=tmp_path)
    _operation_timing(items[0], duration=5)
    for item in items[1:]:
        with pytest.raises(ValueError, match="超出视频时长"):
            _operation_timing(item, duration=5)


@pytest.mark.asyncio
async def test_execution_keeps_failure_rows_history_exports_and_rerun(monkeypatch, tmp_path):
    source = rows()
    source[1]["task_start_time"] = None
    source.append(dict(id="other", query="其他任务", video_path="other.mp4"))
    items, _ = parse(source)
    cfg = load_config(Path("config"))
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "web_history")
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)
    evaluated = []

    async def prepare(_fn, item, **kwargs):
        assert not item.get("input_error")
        return {**item, "frames": ["fake-frame.jpg"]}

    async def evaluate(mode, index, item, *args, **kwargs):
        assert not item.get("input_error")
        evaluated.append(index)
        return {"item_id": item["id"], "query": item["query"], "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "run_video_preparation", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    task = Task(id="isolated-batch", mode="operation", items=items, options={"concurrency": 8})
    await runner.run_eval(task, cfg)
    assert sorted(evaluated) == [0, 3]
    assert task.done_total == 4 and len(task.results) == 4
    by_index = {r["index"]: r for r in task.results}
    assert all(by_index[i]["failure_stage"] == "input_validation" for i in (1, 2))
    assert all(not by_index[i].get("correctness") for i in (1, 2))
    assert by_index[2]["turn_index"] == 3
    snapshot = history.task_to_snapshot(task)
    exported = history.jsonl_export_rows(snapshot)
    assert len(exported) == 4 and exported[1]["evaluation"]["error"]
    assert exported[1]["input_error"] == items[1]["input_error"]
    restored, errors = parse_jsonl(history.rows_to_jsonl(exported), "operation")
    assert len(restored) == 4 and len(errors) == 2
    sheets = history.export_rows(snapshot)
    assert len(sheets["数据集明细"]) == len(sheets["逐题结果"]) == len(sheets["评估结果"]) == 4
    assert [r["query"] for r in sheets["逐题结果"]] == [r["query"] for r in source]
    await runner.run_rerun(task, cfg, [1, 2])
    assert sorted(evaluated) == [0, 3] and len(task.results) == 4
    assert task.results[1]["error"]  # Bad original input cannot disappear on rerun.


@pytest.mark.asyncio
async def test_batch_submission_accepts_raw_invalid_conversation_and_records_all_rows(monkeypatch, tmp_path):
    source = rows()
    source[1]["task_end_time"] = None
    source.append(dict(id="good", query="独立单轮", video_path="single.mp4"))
    cfg = load_config(Path("config"))
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    monkeypatch.setattr(server, "_normalize_eval_options", lambda config, options: (dict(options), config))
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "web_history")
    captured = []

    async def run(task, config):
        captured.extend(task.items)

    monkeypatch.setattr(server, "run_eval", run)
    result = await server.api_eval(server.EvalReq(mode="operation", items=source, options={"concurrency": 8}))
    task = tasks.TASKS[result["task_id"]]
    await task.execution
    assert len(captured) == 4
    assert not captured[0].get("input_error") and not captured[3].get("input_error")
    assert captured[1]["input_error"] and captured[2]["input_error"]
    assert len(task.items) == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js")
def test_web_keeps_failed_items_selectable_and_preserves_validation_fields():
    source = rows()
    source[1].pop("task_end_time")
    items, _ = parse(source)
    script = Path("src/auto_eval/web/static/app.js").read_text()
    start = script.index("          opItems.value = imported.map((item) => {")
    mapping = script[start:script.index("          importedDatasetSelections.value =", start)]
    start = script.index("        items.value = valid.map(({ it, sourceIndex }) => {")
    submission = script[start:script.index("        errors.value = [];", start)]
    program = "const assert=require('assert').strict, mode={value:'operation'},opItems={value:[]},items={value:[]}; const newOpItem=()=>({}); const imported=" + json.dumps(items) + ";\n" + mapping
    program += "const valid=opItems.value.map((it,sourceIndex)=>({it,sourceIndex}));\n" + submission
    program += "assert.equal(items.value.length,3); assert(items.value[1].input_error); assert.equal(items.value[2].turn_index,3); assert.equal(items.value[2].evaluation_scope,'latest_turn');"
    subprocess.run([shutil.which("node"), "-e", program], check=True, capture_output=True, text=True)
    assert "it.inputError || (it.query.trim()" in script
