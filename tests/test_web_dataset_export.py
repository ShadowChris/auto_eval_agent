import json
import zipfile
from pathlib import Path

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
                "issue_types": [],
                "total": 5,
                "rubric": {"操作完成度": 5},
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
    assert dataset[0]["帧项目相对路径"].splitlines() == [
        "runs/videos/imported/session/001_op_1/kf_001.jpg",
        "runs/videos/imported/session/001_op_1/kf_002.jpg",
    ]

    results = sheets["逐题结果"]
    assert [row["item_id"] for row in results] == ["op_1", "op_2", "op_3"]
    assert results[0]["correctness"] == "ok"
    assert results[1]["评估状态"] == "评估失败"
    assert "correctness" not in results[1]
    assert results[2]["评估状态"] == "待评估"
    assert "correctness" not in results[2]

    frames = sheets["抽帧清单"]
    assert frames[0]["时间点"] == 1.5
    assert frames[0]["保留原因"] == "scene-change"
    assert frames[-1]["抽帧状态"] == "无抽帧结果"


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
