"""Each Query is evaluated against its inherited requirement, never future inputs."""
import copy
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd
from fastapi.testclient import TestClient
from PIL import Image

from auto_eval.config import load_config
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.judges.prompts import OPERATION_SYSTEM
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.operation_conversation import group_operation_items, normalize_operation_items, validate_round_prefixes
from auto_eval.web import history, server
from auto_eval.web.operation_media import prepare_operation_query_images, prepare_session_operation_item
from auto_eval.web.operation_runs import evaluation_snapshot, freeze_frames, validate_conversation_snapshot
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.runner import _to_evalitem


def source_rows():
    return [
        dict(id="r1", query="帮我设闹钟", answer="请问几点？", video_path="data/all.mp4",
             task_start_time=0, task_end_time=4, is_start=True, is_end=False, 标签="原始1"),
        dict(id="r2", query="早上七点", answer="已设置七点闹钟", video_path="",
             task_start_time=4, task_end_time=8, is_start=False, is_end=False, 标签="原始2"),
        dict(id="r3", query="取消刚才的闹钟", answer="已取消", video_path="",
             task_start_time=8, task_end_time=12, is_start=False, is_end=True, 标签="原始3"),
    ]


def test_single_turn_system_prompt_preserves_pre_multiturn_rendering():
    """Golden HEAD rendering: do not accidentally reorder shared single rules.

    Intentional policy changes may update this digest with standards/regressions.
    """
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    skill = cfg.domain_skills["operation"]
    rendered = OPERATION_SYSTEM.render(persona="终端用户", dims=skill.rubrics, scale=5,
        policy=skill.operation_policy, expert_knowledge_text="", multi_group=False,
        has_query_images=False, multi_turn=False, latest_turn=False)
    assert hashlib.sha256(rendered.encode()).hexdigest() == "39a20b2cb9a93fa387e2be2d5cd9768f19237d24b8b2dd87e27e1c58d45b06e8"


def parsed(rows=None):
    items, errors = parse_jsonl("\n".join(json.dumps(row, ensure_ascii=False) for row in (rows or source_rows())), "operation")
    assert not errors, errors
    return items


def test_each_round_keeps_its_own_id_input_and_history_prefix():
    items = parsed()
    assert [i["id"] for i in items] == ["r1", "r2", "r3"]
    assert [len(i["turns"]) for i in items] == [1, 2, 3]
    assert [i["turn_index"] for i in items] == [1, 2, 3]
    assert [i["turn_count"] for i in items] == [3, 3, 3]
    assert [i["source_line"] for i in items] == [1, 2, 3]
    assert [i["source_data"]["标签"] for i in items] == ["原始1", "原始2", "原始3"]
    assert all(i["evaluation_scope"] == "latest_turn" and i["case_id"] == "r1" for i in items)
    assert items[1]["query"] == "早上七点"
    assert items[1]["turns"][0]["answer"] == "请问几点？"
    assert "取消" not in json.dumps(items[1], ensure_ascii=False)
    assert normalize_operation_items(items) == items
    items[0]["turns"][0]["answer"] = "changed"
    assert items[1]["turns"][0]["answer"] == "请问几点？"


def test_uppercase_markers_accept_the_same_recording_url_per_row():
    source = source_rows()
    url = "https://example.com/conversation.mp4"
    for row in source:
        row.update(video_path=url, is_start=str(row["is_start"]).upper(), is_end=str(row["is_end"]).upper())
    items = parsed(source)
    assert [i["video_path"] for i in items] == [url, url, url]
    assert [i["turn_index"] for i in items] == [1, 2, 3]
    assert [len(i["turns"]) for i in items] == [1, 2, 3]


def test_round_attachment_domain_ip_fallback_retains_history_only(tmp_path, monkeypatch):
    from auto_eval.web import operation_media
    image = tmp_path / "downloaded.png"
    Image.new("RGB", (20, 20)).save(image)
    source = source_rows()
    domain, ip = "https://example.com/query.png", "http://10.0.0.2/query.png"
    source[0].update(attachment_path="/ignored.png", metadata={"attachment_url_domain":domain,"attachment_url_ip":ip})
    attempts = []

    def resolve(raw, **kwargs):
        attempts.append(raw)
        if raw == domain:
            raise ValueError("domain unavailable")
        assert raw == ip
        return image

    monkeypatch.setattr(operation_media, "resolve_operation_query_image_path", resolve)
    later = prepare_operation_query_images(parsed(source)[1], base_dir=tmp_path)
    assert attempts == [domain, ip]
    assert later["turns"][0]["query_images"] == [str(image)]
    assert not later["turns"][1].get("query_images")
    assert not later.get("query_images")


def test_generated_ids_and_structured_prefix_single_api():
    turns = source_rows()
    for turn in turns:
        turn.pop("id")
    batch = normalize_operation_items([dict(id="chat", video_path="data/all.mp4", turns=turns)])
    assert [i["id"] for i in batch] == ["chat_turn_001", "chat_turn_002", "chat_turn_003"]
    single = server._normalize_single_operation_item(dict(id="latest-call", video_path="data/all.mp4", turns=turns))
    assert single["id"] == "latest-call" and single["turn_index"] == 3
    assert normalize_operation_items([single])[0]["id"] == "latest-call"


@pytest.mark.parametrize("business_ids", [["business-a", "business-b", "business-c"], ["", None, 0]])
def test_business_case_ids_do_not_define_or_validate_round_groups(business_ids):
    source = source_rows()
    for row, case_id in zip(source, business_ids):
        row["case_id"] = case_id
        row["is_start"] = str(row["is_start"]).upper()
        row["is_end"] = str(row["is_end"]).upper()
        row["video_path"] = "https://example.com/full.mp4"
    items = parsed(source)
    assert [i["case_id"] for i in items] == business_ids
    assert [i["source_data"]["case_id"] for i in items] == business_ids
    assert [i["conversation_id"] for i in items] == ["r1"] * 3
    assert [len(i["turns"]) for i in items] == [1, 2, 3]
    assert normalize_operation_items(items) == items


@pytest.mark.parametrize("with_ids", [True, False])
def test_repeated_business_case_id_cannot_join_separate_marker_groups(with_ids):
    first = source_rows()
    second = [{**row, "id": "other-" + row["id"], "query": "另一会话：" + row["query"]} for row in source_rows()]
    for row in first + second:
        row["case_id"] = "same-business-id"
        if not with_ids:
            row.pop("id")
    items = parsed(first + second)
    assert len(items) == 6 and len({i["id"] for i in items}) == 6
    assert len({i["conversation_id"] for i in items}) == 2
    assert items[0]["conversation_id"] != items[3]["conversation_id"]
    assert [len(i["turns"]) for i in items] == [1, 2, 3, 1, 2, 3]
    assert all(i["case_id"] == "same-business-id" for i in items)
    assert "另一会话" not in json.dumps(items[2], ensure_ascii=False)


def test_explicit_turns_do_not_require_matching_business_case_ids():
    source = source_rows()
    for index, row in enumerate(source):
        row["case_id"] = "business-" + str(index)
    items = normalize_operation_items([dict(id="root",case_id="parent-business",video_path="data/all.mp4",turns=source)])
    assert [i["case_id"] for i in items] == [r["case_id"] for r in source]
    assert len({i["conversation_id"] for i in items}) == 1


def test_single_api_retains_outer_business_case_id_without_using_it_as_group_key():
    normalized = server._normalize_single_operation_item(dict(id="current-call",case_id="business-outer",
        video_path="data/all.mp4",turns=source_rows()))
    assert normalized["id"] == "current-call"
    assert normalized["case_id"] == normalized["source_data"]["case_id"] == "business-outer"
    assert normalized["conversation_id"] == "r1"
    assert normalize_operation_items([normalized])[0]["case_id"] == "business-outer"


def test_legacy_canonical_prefix_without_conversation_id_remains_readable():
    items = parsed()
    for item in items:
        item.pop("conversation_id")
    normalized = normalize_operation_items(items)
    assert [i["conversation_id"] for i in normalized] == ["r1"] * 3
    assert [i["id"] for i in normalized] == ["r1", "r2", "r3"]


def test_legacy_prefix_consistency_still_matches_first_turn_after_business_id_decoupling():
    source = source_rows()
    for row in source:
        row["case_id"] = "old-business-group"
    items = parsed(source)
    legacy = copy.deepcopy(items[0])
    legacy.pop("conversation_id")
    assert normalize_operation_items([legacy])[0]["conversation_id"] == "r1"
    validate_round_prefixes([legacy, *items[1:]])
    items[1]["turns"][0]["query"] = "历史输入已修改"
    with pytest.raises(ValueError, match="历史输入不一致"):
        validate_round_prefixes([legacy, items[1]])


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
def test_table_import_uses_uppercase_markers_not_business_case_id(suffix):
    source = source_rows()
    for index, row in enumerate(source):
        row.update(case_id=f"business-{index}", is_start=str(row["is_start"]).upper(),
                   is_end=str(row["is_end"]).upper(), video_path="https://example.com/full.mp4")
    frame = pd.DataFrame(source)
    buffer = io.BytesIO()
    if suffix == "csv":
        content = frame.to_csv(index=False).encode()
    else:
        frame.to_excel(buffer, index=False)
        content = buffer.getvalue()
    with TestClient(server.app) as client:
        response = client.post("/api/operation/import-table", files={"file":(f"rounds.{suffix}",content)})
    payload = response.json()
    assert response.status_code == 200 and not payload["errors"]
    assert payload["count"] == 3
    assert [i["case_id"] for i in payload["items"]] == [r["case_id"] for r in source]
    assert len({i["conversation_id"] for i in payload["items"]}) == 1


def test_business_ids_and_internal_conversation_ids_survive_exports():
    source = source_rows()
    for index, row in enumerate(source):
        row["case_id"] = f"business-{index}"
    data = snapshot()
    data["items"] = parsed(source)
    sheets = history.export_rows(data)
    for name in ("数据集明细", "评估结果", "逐题结果"):
        assert [r["case_id"] for r in sheets[name]] == [r["case_id"] for r in source]
        assert [r["conversation_id"] for r in sheets[name]] == ["r1"] * 3
    exported = history.jsonl_export_rows(data)
    assert [r["case_id"] for r in exported] == [r["case_id"] for r in source]
    assert [r["conversation_id"] for r in exported] == ["r1"] * 3
    restored, errors = parse_jsonl(history.rows_to_jsonl(exported), "operation")
    assert not errors
    assert [i["case_id"] for i in restored] == [r["case_id"] for r in source]
    assert len({i["conversation_id"] for i in restored}) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js")
def test_web_submission_preserves_internal_group_and_business_case_ids():
    source = source_rows()
    for index, row in enumerate(source):
        row["case_id"] = f"business-{index}"
    script = (Path(__file__).resolve().parents[1] / "src/auto_eval/web/static/app.js").read_text()
    start = script.index("          opItems.value = imported.map((item) => {")
    mapping = script[start:script.index("          importedDatasetSelections.value =", start)]
    start = script.index("        items.value = valid.map(({ it, sourceIndex }) => {")
    submission = script[start:script.index("        errors.value = [];", start)]
    program = "const assert=require('assert').strict; const mode={value:'operation'},opItems={value:[]},items={value:[]}; const newOpItem=()=>({}); const imported=" + json.dumps(parsed(source),ensure_ascii=False) + ";\n" + mapping
    program += "const valid=opItems.value.map((it,sourceIndex)=>({it,sourceIndex}));\n" + submission
    program += "assert.deepEqual(items.value.map(i=>i.case_id),['business-0','business-1','business-2']); assert.deepEqual(items.value.map(i=>i.conversation_id),['r1','r1','r1']);"
    subprocess.run([shutil.which("node"), "-e", program], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("change", [
    {"turns": [None], "turn_index": 1}, {"turn_index": True},
    {"turn_count": 0}, {"turn_index": 2},
])
def test_invalid_explicit_prefix_is_rejected(change):
    item = parsed()[0]
    item.update(change)
    with pytest.raises(ValueError):
        normalize_operation_items([item])


@pytest.mark.parametrize("broken", [None, [], "not an object"])
def test_non_object_cannot_silently_split_marked_conversation(broken):
    rows = source_rows()
    rows.insert(1, broken)
    items, errors = parse_jsonl("\n".join(json.dumps(row) for row in rows), "operation")
    assert items == [] and errors


def test_new_rounds_cannot_append_to_whole_conversation_policy():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    frozen = evaluation_snapshot(cfg)
    validate_conversation_snapshot(parsed(), {"evaluation_snapshot": frozen})
    frozen["operation_skill"]["operation_policy"]["multi_turn_evaluation_scope"] = "conversation"
    with pytest.raises(ValueError, match="整会话"):
        validate_conversation_snapshot(parsed(), {"evaluation_snapshot": frozen})
    assert server._dataset_item_keys(parsed()[1]) == {"r2"}


def test_legacy_whole_conversation_cannot_silently_use_latest_policy():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    frozen = evaluation_snapshot(cfg)
    legacy = group_operation_items(source_rows())
    with pytest.raises(ValueError, match="旧整会话输入"):
        validate_conversation_snapshot(legacy, {"evaluation_snapshot": frozen})
    frozen["operation_skill"]["operation_policy"]["multi_turn_evaluation_scope"] = "conversation"
    validate_conversation_snapshot(legacy, {"evaluation_snapshot": frozen})


@pytest.mark.asyncio
async def test_fork_rejects_legacy_units_before_creating_history(monkeypatch):
    from fastapi import HTTPException
    legacy = group_operation_items(source_rows())
    source = dict(mode="operation", items=legacy, options={}, status="done")
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: source)
    monkeypatch.setattr(server, "fork_items", lambda *a, **kw: copy.deepcopy(legacy))
    monkeypatch.setattr(server, "_normalize_eval_options", lambda cfg, opts: (opts, cfg))
    created = []
    monkeypatch.setattr(server, "new_task", lambda *a, **kw: created.append(kw))
    with pytest.raises(HTTPException) as caught:
        await server.api_operation_fork("legacy", server.OperationForkReq(action="reuse_frames", execution_mode="evaluate_only"))
    assert caught.value.status_code == 422 and "旧整会话输入" in caught.value.detail
    assert not created


def test_changing_prior_input_requires_consistent_later_prefixes():
    items = parsed()
    changed = copy.deepcopy(items[0])
    changed["query"] = changed["turns"][0]["query"] = "新问题"
    with pytest.raises(ValueError, match="历史输入不一致"):
        validate_round_prefixes([changed, *items[1:]])
    validate_round_prefixes(items[1:])  # Selecting just a later prefix is allowed.


def snapshot():
    return {"task_id": "rounds", "mode": "operation", "status": "done", "options": {},
            "items": parsed(), "results": [dict(index=n, item_id=f"r{n + 1}", correctness=c,
            issue_types=[], need_review_cate=["待用户澄清"] if n == 0 else [], rationale=f"第{n + 1}轮结果")
            for n, c in enumerate(["no_support", "ok", "ok"])]}


def test_every_export_row_has_its_own_result_and_statistics():
    data = snapshot()
    sheets = history.export_rows(data)
    for name in ("评估结果", "逐题结果", "数据集明细"):
        assert len(sheets[name]) == 3
        assert [r["turn_index"] for r in sheets[name]] == [1, 2, 3]
        assert [r["query"] for r in sheets[name]] == [r["query"] for r in source_rows()]
    assert [r["correctness"] for r in sheets["评估结果"]] == ["no_support", "ok", "ok"]
    assert all("is_result_row" not in r for r in sheets["评估结果"])
    exported = history.jsonl_export_rows(data)
    assert [r["evaluation"]["correctness"] for r in exported] == ["no_support", "ok", "ok"]
    assert [len(r["turns"]) for r in exported] == [1, 2, 3]
    assert history.operation_statistics_payload(data)["statistics"]["total_cases"] == 3


def test_export_reimport_retains_extra_current_columns_and_independent_results():
    from auto_eval.web.operation_comparison_import import import_operation_comparison_file
    data = snapshot()
    exported = history.jsonl_export_rows(data)
    restored, errors = parse_jsonl(history.rows_to_jsonl(exported), "operation")
    assert not errors
    assert [r["source_data"]["标签"] for r in restored] == ["原始1", "原始2", "原始3"]
    assert [r["id"] for r in restored] == ["r1", "r2", "r3"]
    for suffix, content in (("xlsx", history.build_xlsx(data)),
                            ("jsonl", history.rows_to_jsonl(exported).encode())):
        imported = import_operation_comparison_file(f"rounds.{suffix}", content)
        assert imported["summary"]["valid_count"] == 3
        assert [r["id"] for r in imported["rows"]] == ["r1", "r2", "r3"]


def test_failed_subset_only_reruns_selected_round_with_history(tmp_path):
    from scripts.excel_eval_failed_subset import extract_failed_subset
    data = snapshot()
    data["results"][1] = dict(index=1, item_id="r2", error="timeout")
    evaluation = tmp_path / "results.xlsx"
    evaluation.write_bytes(history.build_xlsx(data))
    source = tmp_path / "source.jsonl"
    source.write_text("\n".join(json.dumps(r) for r in source_rows()))
    subset = extract_failed_subset(evaluation, source)
    assert subset.selected_rows == 1
    selected, errors = parse_jsonl(subset.output_path.read_text(), "operation")
    assert not errors
    assert selected[0]["id"] == "r2" and len(selected[0]["turns"]) == 2
    assert selected[0]["turns"][0]["answer"] == "请问几点？"


def test_prefix_frames_reuse_window_cache_and_never_include_future(tmp_path):
    video = tmp_path / "data/all.mp4"
    video.parent.mkdir()
    video.write_bytes(b"fake")
    calls = []

    def extract(video, directory, **kwargs):
        calls.append((kwargs["task_start_time"], kwargs["task_end_time"]))
        frame = directory / "kf_001.jpg"
        Image.new("RGB", (10, 10), "red").save(frame)
        (directory / "keyframes.json").write_text(json.dumps({"selected": [{"time": kwargs["task_end_time"] - 1}]}))
        return [frame]

    for index, item in enumerate(parsed()):
        prepared = prepare_session_operation_item(item, session_name="prefixes", item_index=index, total_items=3,
            base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 12, extract_fn=extract)
        assert [r["turn_index"] for r in prepared["frame_manifest"]] == list(range(1, index + 2))
        assert max(r["time"] for r in prepared["frame_manifest"]) < item["task_end_time"]
        freeze_frames(prepared, runs_dir=tmp_path / "runs")
        if index == 0:
            unsafe = copy.deepcopy(item)
            unsafe.update(frames=[str((tmp_path / "runs" / "videos" / "imported" / "prefixes" / "001_r1" / "kf_001.jpg"))],
                          frame_manifest=[dict(index=1,time=3,turn_index=1)])
            marker = Path(unsafe["frames"][0]).parent / ".complete"
            original = marker.read_text()
            marker.write_text(original.replace("bounded-round-v3", "per-round-v2"))
            with pytest.raises(ValueError, match="旧时间窗策略"):
                freeze_frames(unsafe, runs_dir=tmp_path / "runs")
            marker.write_text(original)
    assert calls == [(0, 4), (4, 8), (8, 12)]


def test_input_images_stay_round_scoped_in_history_and_results(tmp_path):
    image = tmp_path / "input.png"
    Image.new("RGB", (20, 20)).save(image)
    source = source_rows()
    source[0]["query_image_path"] = str(image)
    items = parsed(source)
    later = prepare_operation_query_images(items[1], base_dir=tmp_path)
    assert later.get("query_images") == []
    assert later["turns"][0]["query_images"] == [str(image)]
    assert not later["turns"][1].get("query_images")
    source = source_rows()
    source[2]["query_image_path"] = str(image)
    earlier = prepare_operation_query_images(parsed(source)[0], base_dir=tmp_path)
    assert not earlier.get("query_images")
    assert not earlier["turns"][0].get("query_images")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 FFmpeg")
def test_last_frame_cannot_seek_across_round_boundary(tmp_path):
    video = tmp_path / "boundary.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x240:r=4:d=2",
        "-f", "lavfi", "-i", "color=blue:s=160x240:r=4:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
    turns = source_rows()[:2]
    turns[0].update(video_path=str(video), task_start_time=0, task_end_time=1.99)
    turns[1].update(task_start_time=2, task_end_time=3.9, is_end=True)
    item = parsed(turns)[0]
    prepared = prepare_session_operation_item(item, session_name="boundary", item_index=0, total_items=2,
        base_dir=tmp_path, runs_dir=tmp_path / "runs")
    for frame in prepared["frames"]:
        with Image.open(frame) as image:
            red, _, blue = image.convert("RGB").getpixel((20, 20))
            assert red > 180 and blue < 60, "未来蓝色轮次不得提前进入红色轮次"


@pytest.mark.asyncio
async def test_prompt_keeps_history_answer_but_only_judges_current(tmp_path):
    item = parsed()[1]
    item["source_data"]["human_label"] = "DO_NOT_LEAK_HUMAN_LABEL"
    item["turns"][0]["source_data"]["human_label"] = "DO_NOT_LEAK_HISTORY_LABEL"
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (20, 20)).save(frame)
    item.update(frames=[str(frame)], frame_manifest=[dict(index=1, turn_index=2, time=7)])
    captured = {}

    class Client:
        persona = "终端用户"
        cfg = SimpleNamespace(name="judge", persona="end_user")

        async def complete(self, system, user, **kwargs):
            captured.update(system=system, user=user, **kwargs)
            return SimpleNamespace(content=json.dumps(dict(correctness="ok", rubric={}, total=5, issue_types=[], need_review_cate=[], rationale="本轮完成")),
                rounds=1, used_search=False, tool_trace=[], search_queries=[], truncated=False)

    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    judge = RubricJudge(Client(), cfg.rubrics, skill_router=SkillRouter(cfg.domain_skills))
    await judge.score(_to_evalitem(item, 0), "agent", item["answer"], eval_mode="operation")
    assert "请问几点？" in captured["user"] and "当前待评：第2轮" in captured["user"]
    assert "历史背景，不独立评分" in captured["user"]
    assert "取消刚才" not in str(captured)
    assert "DO_NOT_LEAK" not in str(captured)
    assert "不重复处罚历史独立任务" in captured["system"]
    assert "只对当前轮执行原始 Query" in captured["system"]
    assert "不处罚历史回复质量" in captured["system"]
