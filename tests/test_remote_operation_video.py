from __future__ import annotations

import asyncio
from dataclasses import replace
import ipaddress
import json
import os
from pathlib import Path
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from auto_eval.table_dataset import convert_table
from auto_eval.web import remote_video, operation_media, history, server


@pytest.fixture
def network(monkeypatch):
    """固定 DNS 和 HTTP transport，所有普通测试均不访问真实网络。"""
    def dns(host, port, **_kwargs):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = "10.0.0.1" if host == "internal.test" else "93.184.216.34"
        return [(2, 1, 6, "", (address, port))]

    monkeypatch.setattr(remote_video.socket, "getaddrinfo", dns)
    original = httpx.Client

    def install(handler):
        monkeypatch.setattr(remote_video.httpx, "Client", lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs,
        ))
    return install


def test_download_preserves_signed_query_and_pins_checked_ip(tmp_path, network):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"video", headers={"content-type": "application/octet-stream"})
    network(handler)
    video = remote_video.download_remote_video("https://cdn.test/download?token=secret", tmp_path)
    assert video.read_bytes() == b"video"
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "cdn.test"
    assert requests[0].url.query == b"token=secret"
    assert requests[0].extensions["sni_hostname"] == "cdn.test"
    assert not (tmp_path / "video.part").exists()


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://169.254.169.254/a", "http://internal.test/a", "https://user:password@cdn.test/a"])
def test_unsafe_urls_are_blocked_before_request(tmp_path, network, url):
    network(lambda _: pytest.fail("不应发送不安全请求"))
    with pytest.raises(ValueError):
        remote_video.download_remote_video(url, tmp_path)


def test_redirect_target_is_checked_and_explicit_internal_host_can_be_allowed(tmp_path, network):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "http://internal.test/file.mp4"})
        return httpx.Response(200, content=b"video")
    network(handler)
    with pytest.raises(ValueError, match="非公网"):
        remote_video.download_remote_video("https://cdn.test/file.mp4", tmp_path)
    assert len(requests) == 1
    requests.clear()
    cfg = replace(remote_video.RemoteVideoConfig(), allowed_hosts=("internal.test",))
    assert remote_video.download_remote_video("https://cdn.test/file.mp4", tmp_path, config=cfg).is_file()
    assert requests[-1].url.host == "10.0.0.1"


@pytest.mark.parametrize("status,body,headers,expected", [
    (403, b"denied", {}, "HTTP 403"),
    (404, b"missing", {}, "HTTP 404"),
    (200, b"", {}, "空文件"),
    (200, b"html", {"content-type": "text/html"}, "不是视频"),
    (200, b"video", {"content-length": "1000000000"}, "大小限制"),
])
def test_download_failures_have_clear_errors_and_no_partial_files(tmp_path, network, status, body, headers, expected):
    network(lambda _: httpx.Response(status, content=body, headers=headers))
    with pytest.raises(ValueError, match=expected) as error:
        remote_video.download_remote_video("https://cdn.test/file?token=secret", tmp_path)
    assert "secret" not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_streaming_size_limit_and_connection_retry(tmp_path, network):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("failed", request=request)
        return httpx.Response(200, content=b"x" * 2048)
    network(handler)
    cfg = replace(remote_video.RemoteVideoConfig(), max_size_mb=0.001, retry_delay_s=0)
    with pytest.raises(ValueError, match="大小限制"):
        remote_video.download_remote_video("https://cdn.test/video", tmp_path, config=cfg)
    assert len(calls) == 2
    assert not list(tmp_path.iterdir())


def test_prepare_remote_operation_cleans_temp_video_and_keeps_url(tmp_path, monkeypatch):
    downloaded = []
    url = "https://cdn.test/video.mp4?token=signed"
    def download(raw, directory):
        assert raw == url
        video = directory / "video.mp4"
        video.write_bytes(b"video")
        downloaded.append(video)
        return video
    def extract(video, directory, **kwargs):
        assert video.exists()
        frame = directory / "kf_001.jpg"
        frame.write_bytes(b"frame")
        return [frame]
    monkeypatch.setattr(remote_video, "download_remote_video", download)
    prepared = operation_media.prepare_session_operation_item(
        {"id": "001", "query": "打开设置", "video_path": url},
        session_name="session", item_index=0, total_items=1, base_dir=tmp_path,
        runs_dir=tmp_path / "runs", probe_fn=lambda _: 15, extract_fn=extract,
    )
    assert prepared["video_path"] == url
    assert prepared["media"] == [url]
    assert prepared["frame_count"] == 1
    assert Path(prepared["frames"][0]).is_file()
    assert not downloaded[0].exists()
    assert not list((tmp_path / "runs" / "downloads").iterdir())
    assert "_remote_video_identity" not in prepared


def test_prepare_failure_also_cleans_and_never_deletes_local_video(tmp_path, monkeypatch):
    def download(_, directory):
        path = directory / "video.mp4"
        path.write_bytes(b"broken")
        return path
    monkeypatch.setattr(remote_video, "download_remote_video", download)
    with pytest.raises(ValueError, match="时长为 0"):
        operation_media.prepare_session_operation_item(
            {"video_path": "https://cdn.test/a"}, session_name="s", item_index=0,
            total_items=1, base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 0,
        )
    assert not list((tmp_path / "runs" / "downloads").iterdir())
    local = tmp_path / "local.mp4"
    local.write_bytes(b"user-video")
    with pytest.raises(ValueError):
        operation_media.prepare_session_operation_item(
            {"video_path": str(local)}, session_name="s", item_index=0,
            total_items=1, base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 0,
        )
    assert local.read_bytes() == b"user-video"


def test_startup_cleanup_only_removes_own_stale_directories(tmp_path):
    root = tmp_path / "downloads"
    stale = root / "video_old"
    stale.mkdir(parents=True)
    (stale / "video.part").write_bytes(b"partial")
    (stale / ".operation_video_download").touch()
    os.utime(stale, (0, 0))
    user = root / "video_user_owned"
    user.mkdir()
    os.utime(user, (0, 0))
    fresh = root / "video_fresh"
    fresh.mkdir()
    assert remote_video.cleanup_stale_video_downloads(tmp_path) == 1
    assert fresh.is_dir() and user.is_dir()
    assert not stale.exists()


@pytest.mark.asyncio
async def test_cancelled_preparation_notifies_thread_and_cleans_file(tmp_path, monkeypatch):
    entered = threading.Event()
    finished = threading.Event()
    def download(_, directory):
        (directory / "video.part").write_bytes(b"partial")
        cancel = remote_video._CANCEL.get()
        entered.set()
        assert cancel.wait(5)
        raise ValueError("已取消")
    monkeypatch.setattr(remote_video, "download_remote_video", download)
    def prepare():
        try:
            with remote_video.local_video_input("https://cdn.test/a", tmp_path):
                pass
        finally:
            finished.set()
    task = asyncio.create_task(remote_video.run_video_preparation(prepare))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 2)
    assert not list((tmp_path / "downloads").iterdir())


def test_table_import_does_not_report_remote_video_as_missing(tmp_path):
    import pandas as pd

    source = tmp_path / "dataset.xlsx"
    url = "https://cdn.test/a?token=signed"
    pd.DataFrame([{"id": "001", "query": "q", "video_path": url}]).to_excel(source, index=False)
    result = convert_table(source, project_root=tmp_path)
    assert result.rows[0]["video_path"] == url
    assert not result.missing_video_ids
    assert not result.warnings


def test_remote_export_redirects_while_local_export_is_preserved(tmp_path, monkeypatch):
    url = "https://cdn.test/a?token=signed"
    local = tmp_path / "local.mp4"
    local.write_bytes(b"video")
    snapshot = {"items": [{"video_path": url}, {"video_path": str(local)}]}
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(server, "_resolve_operation_video_path", lambda _: local)
    with TestClient(server.app) as client:
        response = client.get("/api/eval/test/items/0/export?format=video", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == url
        assert client.get("/api/eval/test/items/1/export?format=video").content == b"video"
    assert history._project_relative_path(url) == ""


def test_compact_history_keeps_only_remote_video_reference():
    snapshot = {
        "mode": "operation", "status": "done", "results": [],
        "items": [
            {"id": "remote", "video_path": "https://cdn.test/a?token=signed", "frames": ["large.jpg"]},
            {"id": "local", "video_path": "/large/local.mp4", "frames": ["large.jpg"]},
        ],
    }
    items = history.snapshot_payload(snapshot, compact=True)["items"]
    assert items[0]["video_path"] == "https://cdn.test/a?token=signed"
    assert "video_path" not in items[1]
    assert all("frames" not in item for item in items)


def test_multigroup_video_export_resolves_each_group(tmp_path, monkeypatch):
    url = "https://cdn.test/group-b"
    local = tmp_path / "group-a.mp4"
    local.write_bytes(b"local-a")
    snapshot = {"items": [{"group_variants": [
        {"item": {"video_path": str(local)}}, {"item": {"video_path": url}},
    ]}]}
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(server, "_resolve_operation_video_path", lambda _: local)
    with TestClient(server.app) as client:
        assert client.get("/api/eval/t/items/0/export?format=video&group_index=0").content == b"local-a"
        response = client.get("/api/eval/t/items/0/export?format=video&group_index=1", follow_redirects=False)
        assert response.headers["location"] == url
        assert client.get("/api/eval/t/items/0/export?format=video&group_index=2").status_code == 404


def test_download_concurrency_is_separate_from_model_concurrency(tmp_path, network):
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()
    active = 0
    peak = 0
    def handler(_):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return httpx.Response(200, content=b"video")
    network(handler)
    cfg = replace(remote_video.RemoteVideoConfig(), download_concurrency=2)
    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(
            lambda i: remote_video.download_remote_video("https://cdn.test/video", tmp_path / str(i), config=cfg),
            range(8),
        ))
    assert peak == 2
    assert all(path.read_bytes() == b"video" for path in paths)
