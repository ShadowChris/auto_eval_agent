import json
from pathlib import Path

import pytest

from auto_eval.web import server
from auto_eval.web.operation_media import prepare_session_operation_item
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.server import OperationPrepareReq


def test_operation_jsonl_normalizes_manifest_fields():
    content = "\n".join([
        json.dumps({
            "id": "op_001",
            "query": "设置早上七点的闹钟",
            "context": "当前时间22:00",
            "video_path": "data/videos/alarm.mp4",
            "task_start_time": 0,
            "task_end_time": 18.5,
            "agent_statement": "已设置完成",
        }, ensure_ascii=False),
        json.dumps({
            "query": "关闭无线局域网",
            "video_path": "data/videos/wifi.mp4",
        }, ensure_ascii=False),
    ])

    items, errors = parse_jsonl(content, "operation")

    assert not errors
    assert items[0] == {
        "id": "op_001",
        "query": "设置早上七点的闹钟",
        "context": "当前时间22:00",
        "video_path": "data/videos/alarm.mp4",
        "category": "operation",
        "source_line": 1,
        "task_start_time": 0.0,
        "task_end_time": 18.5,
        "answer": "已设置完成",
    }
    assert items[1]["category"] == "operation"
    assert "context" not in items[1]
    assert "answer" not in items[1]


def test_operation_jsonl_task_times_are_optional_and_strictly_validated():
    content = "\n".join([
        '{"query":"default","video_path":"default.mp4"}',
        '{"query":"null","video_path":"null.mp4","task_start_time":null,"task_end_time":null}',
        '{"query":"string","video_path":"a.mp4","task_start_time":"7"}',
        '{"query":"bool","video_path":"b.mp4","task_start_time":true}',
        '{"query":"negative","video_path":"c.mp4","task_end_time":-1}',
        '{"query":"order","video_path":"d.mp4","task_start_time":10,"task_end_time":10}',
        '{"query":"before-default","video_path":"e.mp4","task_end_time":5}',
    ])

    items, errors = parse_jsonl(content, "operation")

    assert [item["query"] for item in items] == ["default", "null"]
    assert all("task_start_time" not in item for item in items)
    assert all("task_end_time" not in item for item in items)
    assert len(errors) == 5
    assert "task_start_time 必须是有限数字" in errors[0]
    assert "task_start_time 必须是有限数字" in errors[1]
    assert "task_end_time 不能小于 0" in errors[2]
    assert "task_end_time 必须大于 task_start_time" in errors[3]
    assert "task_end_time 必须大于 task_start_time" in errors[4]


def test_operation_jsonl_reports_invalid_rows_without_losing_valid_rows():
    content = "\n".join([
        '{"id":"same","query":"q1","video_path":"a.mp4"}',
        '{"id":"same","query":"q2","video_path":"b.mp4"}',
        '{"query":"q3"}',
        '{"query":"q4","video_path":"d.mp4","agent_statement":42}',
    ])

    items, errors = parse_jsonl(content, "operation")

    assert [item["query"] for item in items] == ["q1"]
    assert len(errors) == 3
    assert "id 重复" in errors[0]
    assert "缺少 video_path" in errors[1]
    assert "agent_statement 必须是字符串" in errors[2]


def test_prepare_operation_item_resolves_project_relative_path_and_caches_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    video = tmp_path / "data" / "alarm.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    calls = []

    def fake_extract(path, out_dir):
        calls.append(Path(path))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = out_dir / "kf_001.jpg"
        frame.write_bytes(b"jpg")
        return [frame]

    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(server, "probe_duration", lambda _: 12.5)
    monkeypatch.setattr(server, "extract_scene_keyframes", fake_extract)
    monkeypatch.delenv("OPERATION_VIDEO_ROOTS", raising=False)

    first = server._prepare_operation_item({"query": "q", "video_path": "data/alarm.mp4"})
    second = server._prepare_operation_item({"query": "q", "video_path": "data/alarm.mp4"})

    assert first["media"] == [str(video)]
    assert first["frame_count"] == 1
    assert first["duration"] == 12.5
    assert second["frames"] == first["frames"]
    assert calls == [video]


def test_operation_video_path_cannot_escape_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"fake")
    monkeypatch.setattr(server, "BASE_DIR", project)
    monkeypatch.delenv("OPERATION_VIDEO_ROOTS", raising=False)

    with pytest.raises(ValueError, match="不在允许目录"):
        server._resolve_operation_video_path(str(outside))


@pytest.mark.asyncio
async def test_batch_prepare_isolates_item_errors(monkeypatch: pytest.MonkeyPatch):
    def fake_prepare(item):
        if item["query"] == "bad":
            raise ValueError("视频不存在")
        return {**item, "frames": ["one.jpg"], "media": ["one.mp4"]}

    monkeypatch.setattr(server, "_prepare_operation_item", fake_prepare)
    response = await server.api_prepare_operation(OperationPrepareReq(items=[
        {"id": "ok", "query": "good", "video_path": "good.mp4"},
        {"id": "broken", "query": "bad", "video_path": "bad.mp4"},
    ]))

    assert response["count"] == 1
    assert response["failed"] == 1
    assert response["items"][0]["id"] == "ok"
    assert "broken：视频不存在" in response["errors"][0]


def test_session_prepare_uses_history_name_and_one_based_item_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    video = project / "data" / "slow_query_001.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    extracted_to = []

    def fake_extract(path, out_dir):
        out_dir = Path(out_dir)
        extracted_to.append(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = out_dir / "kf_001.jpg"
        frame.write_bytes(b"jpg")
        return [frame]

    monkeypatch.delenv("OPERATION_VIDEO_ROOTS", raising=False)
    item = {"id": "slow_query_001", "query": "q", "video_path": "data/slow_query_001.mp4"}
    first = prepare_session_operation_item(
        item,
        session_name="20260717_103930_operation_aa5cd32001ec",
        item_index=0,
        total_items=55,
        base_dir=project,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 8.5,
        extract_fn=fake_extract,
    )
    second = prepare_session_operation_item(
        item,
        session_name="20260717_103930_operation_aa5cd32001ec",
        item_index=0,
        total_items=55,
        base_dir=project,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 8.5,
        extract_fn=fake_extract,
    )

    expected_dir = (
        tmp_path / "runs" / "videos" / "imported"
        / "20260717_103930_operation_aa5cd32001ec" / "001_slow_query_001"
    )
    assert extracted_to == [expected_dir]
    assert first["frames"] == [str(expected_dir / "kf_001.jpg")]
    assert second["frames"] == first["frames"]
    assert first["media"] == [str(video)]


def test_session_prepare_passes_task_times_and_invalidates_parameter_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    video = project / "data" / "fast_query_007.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    calls = []

    def fake_extract(path, out_dir, **kwargs):
        calls.append(kwargs)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = out_dir / "kf_001.jpg"
        frame.write_bytes(b"jpg")
        return [frame]

    monkeypatch.delenv("OPERATION_VIDEO_ROOTS", raising=False)
    common = {
        "id": "fast_query_007",
        "query": "q",
        "video_path": "data/fast_query_007.mp4",
        "task_start_time": 0.0,
    }
    first = prepare_session_operation_item(
        {**common, "task_end_time": 12.0},
        session_name="timing-cache",
        item_index=0,
        total_items=1,
        base_dir=project,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 20.0,
        extract_fn=fake_extract,
    )
    second = prepare_session_operation_item(
        {**common, "task_end_time": 14.0},
        session_name="timing-cache",
        item_index=0,
        total_items=1,
        base_dir=project,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 20.0,
        extract_fn=fake_extract,
    )

    assert calls == [
        {"task_start_time": 0.0, "task_end_time": 12.0},
        {"task_start_time": 0.0, "task_end_time": 14.0},
    ]
    assert first["task_start_time"] == 0.0
    assert first["task_end_time"] == 12.0
    assert second["task_end_time"] == 14.0


def test_session_prepare_rejects_task_time_outside_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    video = project / "data" / "short.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    extracted = False

    def fake_extract(path, out_dir, **kwargs):
        nonlocal extracted
        extracted = True
        return []

    monkeypatch.delenv("OPERATION_VIDEO_ROOTS", raising=False)
    with pytest.raises(ValueError, match="超出视频时长"):
        prepare_session_operation_item(
            {
                "query": "q",
                "video_path": "data/short.mp4",
                "task_start_time": 11.0,
            },
            session_name="invalid-timing",
            item_index=0,
            total_items=1,
            base_dir=project,
            runs_dir=tmp_path / "runs",
            probe_fn=lambda _: 10.0,
            extract_fn=fake_extract,
        )
    assert not extracted
