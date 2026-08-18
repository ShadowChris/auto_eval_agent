import json
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pytest

from auto_eval.web import history, server


def _snapshot(project: Path) -> dict:
    video_1 = project / "data" / "videos" / "one.mp4"
    video_2 = project / "data" / "videos" / "two.mp4"
    frame_dir = project / "runs" / "videos" / "imported" / "session" / "001_op_1"
    frame_1 = frame_dir / "kf_001.jpg"
    frame_2 = frame_dir / "kf_002.jpg"
    video_1.parent.mkdir(parents=True)
    frame_dir.mkdir(parents=True)
    video_1.write_bytes(b"video")
    video_2.write_bytes(b"video")
    frame_1.write_bytes(b"frame-1")
    frame_2.write_bytes(b"frame-2")
    (frame_dir / "keyframes.json").write_text(
        json.dumps({
            "video": str(video_1),
            "selected": [
                {"index": 1, "time": 1.5, "source": "scene", "keep_reason": "scene-change"},
                {"index": 2, "time": 3.0, "source": "terminal", "keep_reason": "final-frame"},
            ],
        }),
        encoding="utf-8",
    )
    return {
        "task_id": "task-1",
        "dataset_name": "operation_cases.jsonl",
        "mode": "operation",
        "items": [
            {
                "id": "op_1",
                "query": "打开设置",
                "source_line": 3,
                "source_data": {
                    "id": "op_1",
                    "序号": "simple_001",
                    "session_id": "session-001",
                    "query": "打开设置",
                    "分享链接": "https://example.test/1",
                    "video_path": "data/videos/one.mp4",
                    "custom_field": "kept",
                },
                "video_path": str(video_1),
                "frames": [str(frame_1), str(frame_2)],
                "frame_count": 2,
                "duration": 4.5,
            },
            {
                "id": "op_2",
                "query": "关闭设置",
                "source_line": 4,
                "source_data": {
                    "id": "op_2",
                    "序号": "simple_002",
                    "session_id": "session-002",
                    "query": "关闭设置",
                    "分享链接": "",
                    "video_path": "data/videos/two.mp4",
                },
                "video_path": str(video_2),
                "frames": [],
                "frame_count": 0,
            },
            {
                "id": "op_3",
                "query": "打开蓝牙",
                "source_line": 5,
                "source_data": {
                    "id": "op_3",
                    "序号": "simple_003",
                    "session_id": "session-003",
                    "query": "打开蓝牙",
                    "video_path": "data/videos/missing.mp4",
                },
                "video_path": str(project / "data" / "videos" / "missing.mp4"),
            },
        ],
        # 故意使用与输入不同的完成顺序，并让第三条保持无结果。
        "results": [
            {
                "index": 1,
                "item_id": "op_2",
                "query": "关闭设置",
                "error": "provider failed",
            },
            {
                "index": 0,
                "item_id": "op_1",
                "query": "打开设置",
                "correctness": "ok",
                "execution_routes": ["fast_system", "skill"],
                "route_status": "detected",
                "route_evidence": [
                    {
                        "route": "fast_system",
                        "evidence_frames": [2],
                        "evidence": "直接弹出设置卡",
                        "confidence": 0.9,
                    }
                ],
                "route_rationale": "先快系统后 skill。",
                "issue_types": [],
                "total": 5,
                "rubric": {"操作完成度": 5, "步骤正确性": 4},
                "rubric_reasons": {
                    "操作完成度": "已打开设置",
                    "步骤正确性": "路径正确",
                },
            },
        ],
        "summary": {},
        "item_progress": {},
    }


def test_export_keeps_source_fields_paths_and_input_alignment(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)

    sheets = history.export_rows(snapshot)

    dataset = sheets["数据集明细"]
    assert len(dataset) == 3
    assert dataset[0]["分享链接"] == "https://example.test/1"
    assert dataset[0]["custom_field"] == "kept"
    assert dataset[0]["录屏项目相对路径"] == "data/videos/one.mp4"
    assert dataset[0]["抽帧目录项目相对路径"] == (
        "runs/videos/imported/session/001_op_1"
    )
    assert "帧项目相对路径" not in dataset[0]
    assert "抽帧数量" not in dataset[0]
    assert "录屏时长（秒）" not in dataset[0]

    results = sheets["逐题结果"]
    assert [row["item_id"] for row in results] == ["op_1", "op_2", "op_3"]
    assert list(results[0]) == list(history._OPERATION_EXPORT_COLUMNS)
    assert list(results[0])[:7] == [
        "数据集序号",
        "item_id",
        "序号",
        "sessionid",
        "query",
        "video_path",
        "分享链接",
    ]
    assert results[0]["序号"] == "simple_001"
    assert results[0]["sessionid"] == "session-001"
    assert results[0]["video_path"] == "data/videos/one.mp4"
    assert results[0]["分享链接"] == "https://example.test/1"
    assert results[1]["序号"] == "simple_002"
    assert results[1]["video_path"] == "data/videos/two.mp4"
    assert results[1]["分享链接"] == ""
    assert results[0]["correctness"] == "ok"
    assert results[0]["execution_routes"] == "fast_system；skill"
    assert results[0]["链路类型"] == "快系统；技能"
    assert results[0]["route_status"] == "detected"
    assert '"route": "fast_system"' in results[0]["route_evidence"]
    assert results[0]["route_rationale"] == "先快系统后 skill。"
    assert results[0]["理由_操作完成度"] == "已打开设置"
    assert results[0]["理由_步骤正确性"] == "路径正确"
    assert "index" not in results[0]
    assert "has_video" not in results[0]
    assert "tool_trace" not in results[0]
    assert results[1]["评估状态"] == "评估失败"
    assert results[1]["correctness"] == ""
    assert results[2]["评估状态"] == "待评估"
    assert results[2]["correctness"] == ""

    frames = sheets["抽帧清单"]
    assert frames[0]["时间点"] == 1.5
    assert frames[0]["保留原因"] == "scene-change"
    assert "原始video_path" not in frames[0]
    assert frames[-1]["抽帧状态"] == "无抽帧结果"

    assert set(sheets) == {"数据集明细", "逐题结果", "抽帧清单", "运行汇总"}
    run_summary = sheets["运行汇总"][0]
    assert run_summary["total"] == 3
    assert run_summary["done"] == 1
    assert run_summary["failed"] == 1
    assert run_summary["pending"] == 1


def test_operation_export_cleanup_is_decoupled_from_other_modes(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = _snapshot(tmp_path)
    snapshot["mode"] = "single"
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)

    sheets = history.export_rows(snapshot)

    assert sheets["数据集明细"][0]["帧项目相对路径"].splitlines() == [
        "runs/videos/imported/session/001_op_1/kf_001.jpg",
        "runs/videos/imported/session/001_op_1/kf_002.jpg",
    ]
    assert sheets["抽帧清单"][0]["原始video_path"] == "data/videos/one.mp4"
    assert "逐题-default" in sheets
    assert "评估失败" in sheets
    assert "运行信息" in sheets
    assert "运行汇总" not in sheets


def test_old_operation_results_are_converted_only_when_read_or_exported(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    snapshot["results"][1].update({
        "correctness": "partial",
        "error_type": "路径冗余",
    })
    snapshot["results"][1].pop("issue_types", None)

    payload = history.snapshot_payload(snapshot)
    converted = next(row for row in payload["results"] if row.get("item_id") == "op_1")
    assert converted["correctness"] == "ok"
    assert converted["issue_types"] == ["路径冗余"]
    assert "error_type" not in converted

    sheets = history.export_rows(snapshot)
    exported = next(row for row in sheets["逐题结果"] if row.get("item_id") == "op_1")
    assert exported["correctness"] == "ok"
    assert exported["issue_types"] == "路径冗余"

    original = next(row for row in snapshot["results"] if row.get("item_id") == "op_1")
    assert original["correctness"] == "partial"
    assert "issue_types" not in original


def test_write_frames_zip_contains_images_and_manifest(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    archive = tmp_path / "export.zip"

    history.write_frames_zip(snapshot, archive, project_root=tmp_path)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "001_op_1/kf_001.jpg" in names
        assert "001_op_1/kf_002.jpg" in names
        assert "001_op_1/keyframes.json" in names
        manifest = [
            json.loads(line)
            for line in zf.read("manifest.jsonl").decode("utf-8").splitlines()
        ]
        assert manifest[0]["video_project_path"] == "data/videos/one.mp4"
        assert manifest[0]["timestamp"] == 1.5
        assert manifest[0]["keep_reason"] == "scene-change"
        assert any(
            row["id"] == "op_2" and row["status"] == "missing"
            for row in manifest
        )
        metadata = json.loads(zf.read("001_op_1/keyframes.json"))
        assert metadata["video"] == "data/videos/one.mp4"


def test_write_frames_zip_can_export_one_dataset_item(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    archive = tmp_path / "single-item.zip"

    history.write_frames_zip(
        snapshot,
        archive,
        project_root=tmp_path,
        item_indexes={0},
    )

    with zipfile.ZipFile(archive) as zf:
        assert "001_op_1/kf_001.jpg" in zf.namelist()
        assert not any(name.startswith("002_") for name in zf.namelist())
        manifest = [
            json.loads(line)
            for line in zf.read("manifest.jsonl").decode("utf-8").splitlines()
        ]
        assert {row["id"] for row in manifest} == {"op_1"}


def test_load_item_judge_calls_matches_task_and_item_index(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    snapshot.update({
        "task_id": "task-1",
        "session_name": "session-1",
    })
    trace = tmp_path / "runs" / "judge_calls_operation.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_2",
            "model_raw_output": "raw-2",
        },
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_1",
            "model_raw_output": "raw-1",
        },
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 1,
            "item_id": "op_2",
            "judge": "judge_2",
        },
        {
            "task_id": "other-task",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_2",
        },
    ]
    trace.write_text(
        "".join(json.dumps(row) + "\n" for row in records),
        encoding="utf-8",
    )

    payload = history.load_item_judge_calls(
        snapshot,
        0,
        runs_dir=trace.parent,
        project_root=tmp_path,
        trace_paths=[trace],
    )

    assert payload["item_id"] == "op_1"
    assert payload["judge_call_count"] == 2
    assert {
        row["model_raw_output"] for row in payload["judge_calls"]
    } == {"raw-1", "raw-2"}
    assert payload["judge_calls"][0]["_trace_file"] == (
        "runs/judge_calls_operation.jsonl"
    )


def test_item_judge_export_supports_chinese_download_filename(monkeypatch):
    snapshot = {
        "task_id": "task-1",
        "mode": "operation",
        "items": [{"id": "众测_001", "query": "打开设置"}],
        "results": [],
    }
    monkeypatch.setattr(server, "get_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(
        server,
        "load_item_judge_calls",
        lambda _snapshot, _index: {
            "item_id": "众测_001",
            "judge_call_count": 1,
            "judge_calls": [{"model_raw_output": "raw"}],
        },
    )

    response = server.api_export_item("task-1", 0, "judge_calls")

    assert response.status_code == 200
    assert json.loads(response.body)["judge_call_count"] == 1
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    disposition.encode("latin-1")


def test_xlsx_export_filename_contains_dataset_time_and_short_task_id(monkeypatch):
    created_at = 1_786_002_524.0
    snapshot = {
        "task_id": "91f98ac3d82d",
        "dataset_name": "data/0726_v2/任务类_0726_v2_众测.jsonl",
        "created_at": created_at,
        "mode": "operation",
        "items": [],
        "results": [],
    }
    monkeypatch.setattr(server, "get_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(server, "build_xlsx", lambda _snapshot, _cfg: b"xlsx")
    monkeypatch.setattr(server, "cfg", lambda: None)

    response = server.api_export("91f98ac3d82d", "xlsx")

    timestamp = datetime.fromtimestamp(created_at).strftime("%Y%m%d_%H%M%S")
    expected = f"任务类_0726_v2_众测_eval_{timestamp}_91f98ac3.xlsx"
    disposition = response.headers["content-disposition"]
    assert f'filename="eval_{timestamp}_91f98ac3.xlsx"' in disposition
    encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded_name) == expected
    disposition.encode("latin-1")


def test_jsonl_export_keeps_source_fields_and_nests_evaluation(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)
    snapshot.update({
        "session_name": "session-1",
        "status": "running",
        "created_at": 1_786_002_524.0,
        "updated_at": 1_786_002_600.0,
        "options": {
            "judges": ["judge_2"],
            "model": "my_agent",
            "concurrency": 8,
            "eval_timeout_s": 300,
        },
    })
    snapshot["items"][0]["source_data"]["人工标签"] = "重点复核"

    rows = history.jsonl_export_rows(snapshot)

    assert len(rows) == 3
    completed = rows[0]
    assert completed["id"] == "op_1"
    assert completed["人工标签"] == "重点复核"
    assert completed["dataset_index"] == 1
    assert completed["source_line"] == 3
    assert completed["frames_dir"].endswith("001_op_1")
    assert completed["evaluation"]["status"] == "completed"
    assert completed["evaluation"]["correctness"] == "ok"
    assert completed["evaluation"]["rubric"] == {"操作完成度": 5, "步骤正确性": 4}
    assert completed["evaluation"]["rubric_reasons"]["操作完成度"] == "已打开设置"
    assert "query" not in completed["evaluation"]
    assert "item_id" not in completed["evaluation"]
    assert "评估状态" not in completed["evaluation"]
    assert "used_search" not in completed["evaluation"]
    assert completed["eval_run"]["task_id"] == "task-1"
    assert completed["eval_run"]["judges"] == ["judge_2"]

    failed = rows[1]
    assert failed["evaluation"]["status"] == "failed"
    assert failed["evaluation"]["error"] == "provider failed"
    assert failed["evaluation"]["correctness"] is None

    pending = rows[2]
    assert pending["evaluation"]["status"] == "pending"
    assert pending["evaluation"]["issue_types"] == []
    assert pending["evaluation"]["rubric"] == {}

    encoded = history.rows_to_jsonl(rows)
    assert len(encoded.splitlines()) == 3
    assert json.loads(encoded.splitlines()[0])["人工标签"] == "重点复核"


def test_jsonl_export_recovers_sequence_from_legacy_item_id(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    snapshot["items"][0]["id"] = "rom6.1_录屏_simple_001"
    snapshot["items"][0]["source_data"]["id"] = "rom6.1_录屏_simple_001"
    snapshot["results"][0]["item_id"] = "rom6.1_录屏_simple_001"

    rows = history.jsonl_export_rows(snapshot)

    assert rows[0]["序号"] == "simple_001"


def test_jsonl_export_rejects_source_field_name_conflicts(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    snapshot["items"][0]["source_data"]["evaluation"] = {"人工标签": "nok"}

    with pytest.raises(ValueError, match="保留字段.*evaluation"):
        history.jsonl_export_rows(snapshot)


def test_jsonl_export_api_uses_dataset_filename(monkeypatch):
    created_at = 1_786_002_524.0
    snapshot = {
        "task_id": "91f98ac3d82d",
        "dataset_name": "任务类_0726_v2_众测.jsonl",
        "created_at": created_at,
        "mode": "operation",
        "status": "done",
        "items": [{"id": "q1", "query": "打开设置"}],
        "results": [{"index": 0, "item_id": "q1", "correctness": "ok"}],
    }
    monkeypatch.setattr(server, "get_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)

    response = server.api_export("91f98ac3d82d", "jsonl")

    timestamp = datetime.fromtimestamp(created_at).strftime("%Y%m%d_%H%M%S")
    disposition = response.headers["content-disposition"]
    assert f'filename="eval_{timestamp}_91f98ac3.jsonl"' in disposition
    encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded_name) == (
        f"任务类_0726_v2_众测_eval_{timestamp}_91f98ac3.jsonl"
    )


def test_export_rejects_unknown_format_instead_of_returning_csv(monkeypatch):
    snapshot = {
        "task_id": "task-1",
        "mode": "operation",
        "items": [],
        "results": [],
    }
    monkeypatch.setattr(server, "get_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)

    with pytest.raises(server.HTTPException) as exc_info:
        server.api_export("task-1", "json-lines")

    assert exc_info.value.status_code == 400
    assert "json-lines" in str(exc_info.value.detail)
