from pathlib import Path

import pytest
from fastapi import HTTPException

from auto_eval.config import load_config
from auto_eval.web import history, server, video_prepare
from auto_eval.web.parse_input import parse_csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _profile():
    return load_config(PROJECT_ROOT / "config").visual_modes["rich_content"]


def test_csv_keeps_remote_video_path():
    items, errors = parse_csv(
        "query,is_start,is_end,video_path\n"
        "天气,true,true,https://cdn.example.test/weather.mp4?token=abc\n",
        "rich_content",
    )

    assert errors == []
    assert items[0]["video_path"] == (
        "https://cdn.example.test/weather.mp4?token=abc"
    )


def test_download_remote_video_streams_to_local_file(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        url = "https://cdn.example.test/redirected/weather.mp4"
        headers = {"content-type": "video/mp4"}

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"video-"
            yield b"bytes"

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeStream()

    monkeypatch.setattr(video_prepare.httpx, "stream", fake_stream)

    path = video_prepare.download_remote_video(
        "https://cdn.example.test/video?id=1",
        tmp_path / "downloads",
        timeout_s=30,
    )

    assert path.is_file()
    assert path.suffix == ".mp4"
    assert path.read_bytes() == b"video-bytes"
    assert calls[0][0] == "GET"
    assert calls[0][2]["follow_redirects"] is True


def test_prepare_remote_video_preserves_url_and_reports_temporary_file(tmp_path):
    source_url = "https://cdn.example.test/videos/weather.mp4?token=abc"
    used_paths = []

    def fake_download(url, output_dir):
        assert url == source_url
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "weather_downloaded.mp4"
        path.write_bytes(b"video")
        used_paths.append(path)
        return path

    def fake_extract(video_path, output_dir, **kwargs):
        assert video_path == used_paths[0]
        output_dir.mkdir(parents=True, exist_ok=True)
        frame = output_dir / "kf_001.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    prepared = video_prepare.prepare_session_rich_content_item(
        {
            "id": "remote_001",
            "query": "天气",
            "video_path": source_url,
        },
        profile=_profile(),
        session_name="remote-session",
        item_index=0,
        total_items=1,
        base_dir=tmp_path,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 10.0,
        extract_fn=fake_extract,
        download_fn=fake_download,
    )

    assert prepared["video_path"] == source_url
    assert prepared["media"] == [source_url]
    assert prepared["video_name"] == "weather.mp4"
    assert prepared["_temporary_video_paths"] == [str(used_paths[0])]
    assert used_paths[0].is_file()


def test_remote_video_is_deleted_after_prepare_cleanup(tmp_path):
    source_url = "https://cdn.example.test/weather.mp4"
    downloaded = []

    def fake_download(url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "weather.mp4"
        path.write_bytes(b"video")
        downloaded.append(path)
        return path

    def fake_extract(_video_path, output_dir, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        frame = output_dir / "kf_001.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    prepared = video_prepare.prepare_session_rich_content_item(
        {"query": "天气", "video_path": source_url},
        profile=_profile(),
        session_name="remote-session",
        item_index=0,
        total_items=1,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 10.0,
        extract_fn=fake_extract,
        download_fn=fake_download,
    )

    video_prepare.cleanup_item_video_downloads(
        prepared,
        runs_dir=tmp_path / "runs",
    )

    assert not downloaded[0].exists()
    assert "_temporary_video_paths" not in prepared
    assert prepared["video_path"] == source_url


def test_prepare_failure_removes_downloaded_file(tmp_path):
    downloaded = []

    def fake_download(_url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "broken.mp4"
        path.write_bytes(b"video")
        downloaded.append(path)
        return path

    with pytest.raises(ValueError, match="probe failed"):
        video_prepare.prepare_session_rich_content_item(
            {
                "query": "天气",
                "video_path": "https://cdn.example.test/broken.mp4",
            },
            profile=_profile(),
            session_name="remote-session",
            item_index=0,
            total_items=1,
            runs_dir=tmp_path / "runs",
            probe_fn=lambda _: (_ for _ in ()).throw(ValueError("probe failed")),
            download_fn=fake_download,
        )

    assert not downloaded[0].exists()


def test_cleanup_never_removes_user_local_video(tmp_path):
    local_video = tmp_path / "user-video.mp4"
    local_video.write_bytes(b"video")
    item = {"_temporary_video_paths": [str(local_video)]}

    video_prepare.cleanup_item_video_downloads(
        item,
        runs_dir=tmp_path / "runs",
    )

    assert local_video.is_file()


def test_history_keeps_remote_url_without_treating_it_as_project_path(tmp_path):
    source_url = "https://cdn.example.test/videos/weather.mp4"
    snapshot = {
        "task_id": "remote-task",
        "dataset_name": "remote.jsonl",
        "mode": "rich_content",
        "items": [{
            "id": "remote_001",
            "query": "天气",
            "video_path": source_url,
            "source_data": {
                "id": "remote_001",
                "query": "天气",
                "video_path": source_url,
            },
        }],
        "results": [],
        "summary": {},
    }

    sheets = history.export_rows(snapshot)
    row = sheets["数据集明细"][0]

    assert row["video_path"] == source_url
    assert row["录屏项目相对路径"] == ""


def test_result_table_links_directly_to_remote_video_not_export_endpoint():
    app_js = (
        PROJECT_ROOT / "src/auto_eval/web/static/app.js"
    ).read_text(encoding="utf-8")
    index_html = (
        PROJECT_ROOT / "src/auto_eval/web/static/index.html"
    ).read_text(encoding="utf-8")

    assert "function itemVideoLinks(result)" in app_js
    assert 'target="_blank"' in index_html
    assert "itemVideoLinks(r)" in index_html
    assert "itemArtifactUrl(r, 'video')" not in index_html
    assert "原视频链接" in index_html


def test_video_export_endpoint_is_no_longer_supported(monkeypatch):
    snapshot = {
        "task_id": "remote-task",
        "mode": "rich_content",
        "items": [{
            "id": "remote_001",
            "query": "天气",
            "video_path": "https://cdn.example.test/weather.mp4",
        }],
        "results": [],
    }
    monkeypatch.setattr(server, "peek_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _task_id: snapshot)

    with pytest.raises(HTTPException) as exc_info:
        server.api_export_item("remote-task", 0, "video")

    assert exc_info.value.status_code == 400
