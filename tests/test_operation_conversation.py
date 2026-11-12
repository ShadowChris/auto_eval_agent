"""Legacy whole-conversation history compatibility and shared media behavior."""
import copy
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from auto_eval.config import load_config
from auto_eval.analysis.operation_source import shared_input_differences
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.operation_conversation import group_operation_items, normalize_operation_items
from auto_eval.web import history, server
from auto_eval.web.operation_comparison_import import import_operation_comparison_file
from auto_eval.web.operation_media import prepare_operation_query_images, prepare_session_operation_item
from auto_eval.web.operation_runs import freeze_frames, verify_frames, evaluation_snapshot, validate_conversation_snapshot
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.runner import _to_evalitem
from auto_eval.web.tasks import Task


def rows():
    return [
        {"id": "t1", "query": "帮我设一个闹钟", "answer": "请问几点？", "video_path": "data/all.mp4",
         "is_start": True, "is_end": False, "task_start_time": 0, "task_end_time": 8, "标签": "原始一"},
        {"id": "t2", "query": "明早七点", "answer": "已设置", "video_path": "",
         "is_start": False, "is_end": True, "task_start_time": 10, "task_end_time": 20, "标签": "原始二"},
    ]


def parsed(source=None):
    return [{**item, "category": "operation"} for item in group_operation_items(source or rows())]


def snapshot(error=False):
    items = parsed() + [{"id": "single", "query": "打开蓝牙", "video_path": "data/single.mp4"}]
    result = {"index": 0, "item_id": "t1", "correctness": "ok", "total": 5, "rationale": "第2轮完成原目标", "issue_types": [], "need_review_cate": [], "latency_s": 2,
              "rubric": {"操作完成度": 5, "步骤正确性": 5}, "rubric_reasons": {"操作完成度": "两轮闭环", "步骤正确性": "正常澄清"}}
    if error:
        result = {"index": 0, "item_id": "t1", "error": "timeout"}
    return {"task_id": "test-turns", "mode": "operation", "status": "done", "items": items,
            "results": [result, {"index": 1, "item_id": "single", "correctness": "nok", "issue_types": ["任务结果错误"], "rationale": "状态错误"}], "options": {}}


def test_grouping_and_sources_are_lossless_and_idempotent():
    source = rows()
    original = copy.deepcopy(source)
    items = parsed(source)
    assert len(items) == 1
    assert items[0]["id"] == items[0]["case_id"] == "t1"
    assert [t["source_data"]["标签"] for t in items[0]["turns"]] == ["原始一", "原始二"]
    assert items[0]["turns"][1]["source_data"]["video_path"] == ""
    assert group_operation_items(items)[0]["turns"] == items[0]["turns"]
    assert source == original


@pytest.mark.parametrize("change", [
    lambda r: r[0].pop("task_start_time"),
    lambda r: r[1].update(task_end_time=float("nan")),
    lambda r: r[1].update(task_start_time=7),
    lambda r: r[1].update(video_path="other.mp4"),
    lambda r: r[1].update(is_end=False),
    lambda r: r[1].update(is_start=True),
    lambda r: r[0].update(is_start=False),
    lambda r: r[0].update(is_start="maybe"),
    lambda r: r[1].pop("is_start"),
    lambda r: r[1].update(query=""),
])
def test_invalid_conversation_is_retained_with_local_errors_or_recovery_warning(change):
    source = rows()
    change(source)
    items, errors = parse_jsonl("\n".join(json.dumps(row) for row in source), "operation")
    assert len(items) == len(source)
    assert [item["id"] for item in items] == ["t1", "t2"]
    assert errors or any(item.get("input_warnings") for item in items)
    for item in items:
        if item.get("input_error"):
            assert not item.get("turns")


def test_middle_round_and_legacy_single_compatibility():
    source = rows()
    source.insert(1, {**source[1], "id": "middle", "task_start_time": 8, "task_end_time": 10, "is_end": "FALSE"})
    assert len(parsed(source)[0]["turns"]) == 3
    for markers in ({}, {"is_start": True, "is_end": True}):
        single = {"query": "打开设置", "video_path": "v.mp4", **markers}
        assert "turns" not in parsed([single])[0]


def test_structured_api_input_and_explicit_case_id():
    source = rows()
    item = {"id": "whole", "video_path": "data/all.mp4", "turns": source}
    normalized = server._normalize_single_operation_item(item)
    assert normalized["id"] == "whole"
    assert normalized["turn_count"] == 2
    assert _to_evalitem(normalized, 0).turns[1].query == "明早七点"


@pytest.mark.asyncio
async def test_batch_api_submits_each_round(monkeypatch):
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    capture = {}

    def new_task(mode, items, options, **kwargs):
        task = Task(id="conversation-api", mode=mode, items=items, options=options)
        capture["task"] = task
        return task

    async def run_eval(task, config):
        capture["evaluated_count"] = len(task.items)

    monkeypatch.setattr(server, "cfg", lambda: config)
    monkeypatch.setattr(server, "_normalize_eval_options", lambda cfg, opts: (opts, cfg))
    monkeypatch.setattr(server, "new_task", new_task)
    monkeypatch.setattr(server, "run_eval", run_eval)
    response = await server.api_eval(server.EvalReq(mode="operation", items=rows()))
    await capture["task"].execution
    assert response["total"] == 2
    assert capture["evaluated_count"] == 2
    assert capture["task"].items[0]["turn_count"] == 2


def test_append_identity_and_conflict_are_case_scoped():
    item = parsed()[0]
    assert server._dataset_item_keys(item) == {"t1"}
    task = Task(id="old", mode="operation", items=[item], options={}, status="done")
    incoming = parsed([{**row, "query": row["query"] + "改动"} for row in rows()])
    preview = server._build_append_merge_preview(task, incoming)
    assert len(preview["conflicts"]) == 1
    assert not preview["new_items"]


def test_old_policy_snapshot_cannot_silently_judge_new_conversations():
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    saved = evaluation_snapshot(config)
    validate_conversation_snapshot(normalize_operation_items(parsed()), {"evaluation_snapshot": saved})
    old = copy.deepcopy(saved)
    old["operation_skill"]["operation_policy"].pop("multi_turn_rules")
    with pytest.raises(ValueError, match="历史规则快照不支持多轮"):
        validate_conversation_snapshot(normalize_operation_items(parsed()), {"evaluation_snapshot": old})
    validate_conversation_snapshot([{"query": "单轮"}], {"evaluation_snapshot": old})


def test_failed_subset_keeps_every_round(tmp_path):
    from scripts.excel_eval_failed_subset import extract_failed_subset

    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows()))
    evaluation = tmp_path / "results.xlsx"
    evaluation.write_bytes(history.build_xlsx(snapshot(error=True)))
    subset = extract_failed_subset(evaluation, dataset)
    restored = [json.loads(line) for line in subset.output_path.read_text().splitlines()]
    assert restored == rows()
    assert subset.failed_rows == 1
    assert subset.selected_rows == 2


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
def test_table_import_groups_before_selection(suffix):
    frame = pd.DataFrame(rows())
    if suffix == "csv":
        content = frame.to_csv(index=False).encode("utf-8")
    else:
        buffer = io.BytesIO()
        frame.to_excel(buffer, index=False)
        content = buffer.getvalue()
    with TestClient(server.app) as client:
        payload = client.post("/api/operation/import-table", files={"file": (f"turns.{suffix}", content)}).json()
    assert not payload["errors"]
    assert payload["count"] == 2
    assert payload["summary"]["source_rows"] == 2
    assert payload["items"][0]["turn_count"] == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js")
def test_web_import_and_submit_keep_the_structured_rounds():
    script = (Path(__file__).resolve().parents[1] / "src/auto_eval/web/static/app.js").read_text()
    mapping = script[script.index("          opItems.value = imported.map((item) => {"):script.index("          importedDatasetSelections.value =", script.index("          opItems.value = imported.map((item) => {"))]
    start = script.index("        items.value = valid.map(({ it, sourceIndex }) => {")
    submission = script[start:script.index("        errors.value = [];", start)]
    source = json.dumps(parsed(), ensure_ascii=False)
    program = "const assert = require('assert').strict; const mode={value:'operation'}, opItems={value:[]}, items={value:[]}; const newOpItem=()=>({}); const imported=" + source + ";\n" + mapping
    program += "const valid=opItems.value.map((it,sourceIndex)=>({it,sourceIndex}));\n" + submission
    program += "assert.equal(items.value.length,1); assert.equal(items.value[0].turns.length,2); assert.equal(items.value[0].turns[1].query,'明早七点'); assert.equal(items.value[0].case_id,'t1');"
    subprocess.run([shutil.which("node"), "-e", program], check=True, capture_output=True, text=True)


def test_excel_and_jsonl_are_row_aligned_but_statistics_count_cases():
    data = snapshot()
    sheets = history.export_rows(data)
    for name in ("评估结果", "逐题结果", "数据集明细"):
        exported = sheets[name]
        assert len(exported) == 3
        assert [r["query"] for r in exported] == ["帮我设一个闹钟", "明早七点", "打开蓝牙"]
        assert [r["is_result_row"] for r in exported] == [True, False, True]
        assert [r["case_id"] for r in exported] == ["t1", "t1", "single"]
    for key in ("correctness", "issue_types", "rationale", "total", "latency_s", "维度_操作完成度", "理由_操作完成度"):
        assert sheets["评估结果"][1][key] == ""
    assert sheets["评估结果"][0]["correctness"] == "ok"
    assert sheets["逐题结果"][1]["评估状态"] == ""
    assert sheets["数据集明细"][1]["标签"] == "原始二"
    assert data["items"][0].get("is_result_row") is None
    stats = history.operation_statistics_payload(data)
    assert stats["statistics"]["total_cases"] == 2
    exported_jsonl = history.jsonl_export_rows(data)
    assert len(exported_jsonl) == 3
    assert exported_jsonl[1]["evaluation"] is None
    assert exported_jsonl[0]["eval_run"]["total"] == 2


def test_failure_only_populates_anchor_row_and_export_reimports_as_cases():
    data = snapshot(error=True)
    sheets = history.export_rows(data)
    assert sheets["逐题结果"][0]["error"] == "timeout"
    assert sheets["逐题结果"][1]["error"] == ""
    for payload, suffix in ((history.build_xlsx(snapshot()), ".xlsx"),
                            (history.rows_to_jsonl(history.jsonl_export_rows(snapshot())).encode(), ".jsonl")):
        imported = import_operation_comparison_file("results" + suffix, payload)
        assert imported["summary"]["valid_count"] == 2
        assert imported["rows"][0]["id"] == "t1"
        assert imported["rows"][0]["query"] == snapshot()["items"][0]["query"]


def test_pending_export_does_not_invent_completed_results():
    data = snapshot()
    data.update(status="running", results=[])
    sheets = history.export_rows(data)
    assert [r["评估状态"] for r in sheets["逐题结果"]] == ["待评估", "", "待评估"]
    assert all(r["correctness"] == "" for r in sheets["评估结果"])
    exported = history.jsonl_export_rows(data)
    assert exported[1]["evaluation"] is None
    assert exported[0]["evaluation"]["status"] != "done"


@pytest.mark.parametrize("state", ["done", "failed", "pending"])
def test_old_source_evaluations_never_fill_explicit_empty_result_fields(state):
    data = snapshot()
    data['items'][0]['source_data'].update(issue_types='未展示可验证结果', correctness='nok', total=1,
                                           need_review_cate='旧复核原因', rationale='旧评估理由')
    data['items'][0]['turns'][0]['source_data'] = dict(data['items'][0]['source_data'])
    if state == 'failed':
        data['results'][0] = {'index': 0, 'item_id': 't1', 'error': 'timeout'}
    elif state == 'pending':
        data['results'] = []
    sheets = history.export_rows(data)
    row = sheets['评估结果'][0]
    assert row['issue_types'] == ''
    assert row['need_review_cate'] == ''
    assert row['correctness'] == ('ok' if state == 'done' else '')
    assert row['rationale'] == ('第2轮完成原目标' if state == 'done' else '')
    assert row['total'] == (5 if state == 'done' else '')
    assert sheets['数据集明细'][0]['issue_types'] == '未展示可验证结果'


def test_exported_conversation_matches_native_shared_inputs():
    data = snapshot()
    native = history.operation_comparison_batch(data, include_source_data=True)
    imported = import_operation_comparison_file("same.xlsx", history.build_xlsx(data))
    assert shared_input_differences(native["rows"][0], imported["rows"][0]) == []
    imported["rows"][0]["source_data"]["turns"][1]["context"] = "改变了背景"
    assert shared_input_differences(native["rows"][0], imported["rows"][0]) == ["turns"]


def test_extract_each_window_and_archive_mapping(tmp_path):
    video = tmp_path / "data/all.mp4"
    video.parent.mkdir()
    video.write_bytes(b"fake")
    calls = []

    def extract(video_path, directory, **kwargs):
        calls.append(kwargs)
        frame = directory / "kf_001.jpg"
        Image.new("RGB", (20, 20), "red").save(frame)
        (directory / "keyframes.json").write_text(json.dumps({"selected": [{"index": 1, "time": kwargs["task_end_time"] - 1}]}))
        return [frame]

    item = prepare_session_operation_item(parsed()[0], session_name="turns", item_index=0, total_items=1,
        base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 25, extract_fn=extract)
    assert [(r["task_start_time"], r["task_end_time"]) for r in calls] == [(0, 8), (10, 20)]
    assert [r["turn_index"] for r in item["frame_manifest"]] == [1, 2]
    assert [r["time"] for r in item["frame_manifest"]] == [7, 19]
    freeze_frames(item, runs_dir=tmp_path / "runs")
    verify_frames(item, runs_dir=tmp_path / "runs")
    assert json.loads((Path(item["frames"][0]).parent / "keyframes.json").read_text())["selected"][1]["turn_index"] == 2
    changed = copy.deepcopy(item)
    changed["turns"][1]["task_start_time"] = 11
    with pytest.raises(ValueError, match="多轮时间窗"):
        verify_frames(changed, runs_dir=tmp_path / "runs")
    changed = copy.deepcopy(item)
    changed["frame_manifest"][0]["turn_index"] = 2
    with pytest.raises(ValueError, match="多轮帧映射"):
        verify_frames(changed, runs_dir=tmp_path / "runs")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要本地 FFmpeg")
def test_real_ffmpeg_preserves_each_round_of_one_video(tmp_path):
    video = tmp_path / "data/all.mp4"
    video.parent.mkdir()
    subprocess.run([
        shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x240:r=4:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True, capture_output=True)
    source = rows()
    source[0].update(task_start_time=0, task_end_time=1.5)
    source[1].update(task_start_time=2, task_end_time=3.5)
    prepared = prepare_session_operation_item(parsed(source)[0], session_name="actual", item_index=0, total_items=1,
        base_dir=tmp_path, runs_dir=tmp_path / "runs")
    assert {r["turn_index"] for r in prepared["frame_manifest"]} == {1, 2}
    for record in prepared["frame_manifest"]:
        start, end = (0, 1.5) if record["turn_index"] == 1 else (2, 3.5)
        assert start <= record["time"] <= end
    assert len(prepared["frames"]) >= 2


def test_turn_images_do_not_leak_to_earlier_rounds(tmp_path):
    image = tmp_path / "data/input.png"
    image.parent.mkdir()
    Image.new("RGB", (20, 20)).save(image)
    source = rows()
    source[1]["attachment_path"] = str(image)
    item = prepare_operation_query_images(parsed(source)[0], base_dir=tmp_path)
    assert not item.get("query_images")
    assert not item["turns"][0].get("query_images")
    assert item["turns"][1]["query_images"] == [str(image)]


@pytest.mark.asyncio
async def test_prompt_reuses_policy_with_round_scoped_evidence(tmp_path):
    source = rows()
    image = tmp_path / "query.png"
    Image.new("RGB", (20, 20)).save(image)
    source[1]["query_images"] = [str(image)]
    item = normalize_operation_items(parsed(source))[-1]
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (20, 20)).save(frame)
    item.update(frames=[str(frame)], frame_manifest=[{"index": 1, "turn_index": 2, "time": 19}])
    capture = {}

    class Client:
        persona = "终端用户"
        cfg = SimpleNamespace(name="judge", persona="end_user")

        async def complete(self, system, user, **kwargs):
            capture.update(system=system, user=user, **kwargs)
            return SimpleNamespace(content=json.dumps({"correctness": "ok", "task_type": "simple", "rubric": {"操作完成度": 5, "步骤正确性": 5}, "total": 5, "issue_types": [], "need_review_cate": [], "rationale": "第2轮补齐并完成"}), rounds=1, used_search=False, tool_trace=[], search_queries=[], truncated=False)

    config = load_config(Path(__file__).resolve().parents[1] / "config")
    judge = RubricJudge(Client(), config.rubrics, skill_router=SkillRouter(config.domain_skills))
    score = await judge.score(_to_evalitem(item, 0), "agent", item["answer"], eval_mode="operation")
    assert score.correctness == "ok"
    assert "【多轮会话】" in capture["system"]
    assert "忽略此前历史内容和完成后的" not in capture["system"]
    assert "当前待评：第2轮" in capture["user"]
    parts = capture["user_content_parts"]
    assert "第2轮原始用户输入图片" in str(parts)
    assert "录屏关键帧 1；第2轮；视频 19 秒" in str(parts)
    assert capture["user_image_roles"] == ["query_image", "recording_frame"]
