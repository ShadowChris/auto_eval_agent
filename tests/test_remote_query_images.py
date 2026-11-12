"""普通图片链接的导入、下载、失败与预览；使用 MockTransport，不访问真实网络。"""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from auto_eval.table_dataset import convert_table
from auto_eval.web import operation_media, server
from auto_eval.web.parse_input import parse_jsonl


def _image_bytes(format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 15), "orange").save(output, format=format)
    return output.getvalue()


@pytest.fixture
def mock_image_http(monkeypatch):
    calls = []

    def configure(handler):
        @contextmanager
        def stream(method, url, **kwargs):
            calls.append((method, url, kwargs))
            with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, **kwargs) as client:
                with client.stream(method, url) as response:
                    yield response
        monkeypatch.setattr(operation_media.httpx, "stream", stream)
        return calls

    return configure


@pytest.mark.parametrize("url", [
    "https://ordinary.example/image.jpg?signature=secret",
    "http://10.0.0.2/image.jpg",
    "http://127.0.0.1:8080/image",
])
def test_reads_trusted_http_links_without_host_restrictions(tmp_path, mock_image_http, url):
    body = _image_bytes()
    calls = mock_image_http(lambda request: httpx.Response(200, content=body))
    source = {"attachment_path": url, "query_images": [url], "自定义": "原始字段"}
    item = {"query_images": [url], "source_data": source}

    prepared = operation_media.prepare_operation_query_images(item, base_dir=tmp_path)

    path = Path(prepared["query_images"][0])
    assert path.is_relative_to(tmp_path / "runs" / "uploads" / "query_images" / "remote")
    assert path.suffix == ".png"  # 按实际内容识别，不依赖 URL 后缀。
    assert path.read_bytes() == body
    assert prepared["source_data"] == source
    assert item["query_images"] == [url]
    assert source["attachment_path"] == url
    assert calls[0][2]["follow_redirects"] is True
    assert calls[0][2]["timeout"].connect == 10
    # 已准备的本地文件用于重跑，无须再次联网。
    again = operation_media.prepare_operation_query_images(prepared, base_dir=tmp_path)
    assert again["query_images"] == [str(path)]
    assert len(calls) == 1
    assert not list(path.parent.glob("*.part"))


@pytest.mark.parametrize("format,suffix", [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")])
def test_redirects_and_extensionless_links_support_all_image_formats(
    tmp_path, mock_image_http, format, suffix,
):
    body = _image_bytes(format)
    visited = []

    def handler(request):
        visited.append(request.url.path)
        if request.url.path == "/link":
            return httpx.Response(302, headers={"location": "/actual"})
        return httpx.Response(200, content=body)

    mock_image_http(handler)
    path = operation_media.resolve_operation_query_image_path("https://images.example/link", base_dir=tmp_path)
    assert path.suffix == suffix
    assert path.read_bytes() == body
    assert visited == ["/link", "/actual"]


def _assert_no_downloads(tmp_path):
    assert not list((tmp_path / "runs").rglob("remote_*"))


@pytest.mark.parametrize("status", [403, 404, 500])
def test_http_errors_report_status_and_do_not_leave_partial_files(tmp_path, mock_image_http, status):
    mock_image_http(lambda request: httpx.Response(status, text="not available"))
    with pytest.raises(ValueError, match=f"HTTP {status}") as error:
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg?token=secret", base_dir=tmp_path)
    assert "secret" not in str(error.value)
    _assert_no_downloads(tmp_path)


@pytest.mark.parametrize("error_type,message", [
    (httpx.ReadTimeout, "下载超时"), (httpx.ConnectError, "ConnectError"),
])
def test_network_failures_are_explicit(tmp_path, mock_image_http, error_type, message):
    def handler(request):
        raise error_type("network error", request=request)
    mock_image_http(handler)
    with pytest.raises(ValueError, match=message):
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg", base_dir=tmp_path)
    _assert_no_downloads(tmp_path)


def test_total_download_budget_is_checked(tmp_path, mock_image_http, monkeypatch):
    clock = iter([0, 61])
    monkeypatch.setattr(operation_media, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    mock_image_http(lambda request: httpx.Response(200, content=_image_bytes()))
    with pytest.raises(ValueError, match="下载超时"):
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg", base_dir=tmp_path)
    _assert_no_downloads(tmp_path)


@pytest.mark.parametrize("advertised", [True, False])
def test_image_size_limit_applies_with_or_without_content_length(
    tmp_path, mock_image_http, monkeypatch, advertised,
):
    monkeypatch.setattr(operation_media, "MAX_QUERY_IMAGE_BYTES", 10)

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * 6
            yield b"b" * 6

    def handler(request):
        headers = {"content-length": "12"} if advertised else {}
        return httpx.Response(200, headers=headers, stream=Chunks())

    mock_image_http(handler)
    with pytest.raises(ValueError, match="10MB"):
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg", base_dir=tmp_path)
    _assert_no_downloads(tmp_path)


@pytest.mark.parametrize("body", [b"", b"<html>please log in</html>", b"not an image"])
def test_non_image_responses_fail_instead_of_being_ignored(tmp_path, mock_image_http, body):
    mock_image_http(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match="无法解析"):
        operation_media.prepare_operation_query_images({"query_images": ["https://example.test/a.jpg"]}, base_dir=tmp_path)
    _assert_no_downloads(tmp_path)


def test_pixel_limit_and_unsupported_formats_are_rejected(tmp_path, mock_image_http, monkeypatch):
    mock_image_http(lambda request: httpx.Response(200, content=_image_bytes("GIF")))
    with pytest.raises(ValueError, match="不支持的用户输入图片格式"):
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg", base_dir=tmp_path)
    mock_image_http(lambda request: httpx.Response(200, content=_image_bytes()))
    monkeypatch.setattr(operation_media, "MAX_QUERY_IMAGE_PIXELS", 10)
    with pytest.raises(ValueError, match="像素数"):
        operation_media.resolve_operation_query_image_path("https://example.test/a.jpg", base_dir=tmp_path)
    _assert_no_downloads(tmp_path)


def test_failed_second_image_cleans_only_new_remote_files(tmp_path, mock_image_http):
    local = tmp_path / "local.png"
    local.write_bytes(_image_bytes())
    mock_image_http(lambda request: httpx.Response(
        404 if request.url.path == "/bad" else 200, content=_image_bytes(),
    ))
    with pytest.raises(ValueError, match="HTTP 404"):
        operation_media.prepare_operation_query_images({"query_images": [
            str(local), "https://example.test/ok", "https://example.test/bad",
        ]}, base_dir=tmp_path)
    assert local.exists()
    _assert_no_downloads(tmp_path)


@pytest.mark.parametrize("field", ["attachment_url_domain", "attachment_url_ip", "query_image_path"])
def test_jsonl_and_direct_preparation_accept_single_image_aliases(tmp_path, mock_image_http, field):
    url = "https://example.test/input.jpg"
    raw = {"id": "001", "query": "识别图片", "video_path": "data/video.mp4", field: url}
    items, errors = parse_jsonl(json.dumps(raw), "operation")
    assert not errors
    assert items[0]["query_images"] == [url]
    assert items[0]["source_data"][field] == url
    mock_image_http(lambda request: httpx.Response(200, content=_image_bytes()))
    assert Path(operation_media.prepare_operation_query_images(raw, base_dir=tmp_path)["query_images"][0]).is_file()
    empty = {"query_images": [], "query_image_path": " ", "attachment_path": ""}
    assert "query_images" not in operation_media.prepare_operation_query_images(empty, base_dir=tmp_path)


def test_standard_array_keeps_priority_over_single_image_aliases():
    items, errors = parse_jsonl(json.dumps({
        "query": "识别图片", "video_path": "video.mp4",
        "query_images": ["https://example.test/standard.png"],
        "query_image_path": "https://example.test/alias.png", "attachment_path": "https://example.test/attachment.png",
    }), "operation")
    assert not errors
    assert items[0]["query_images"] == ["https://example.test/standard.png"]


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_table_import_keeps_url_and_does_not_download_until_preparation(tmp_path, mock_image_http, suffix):
    pd = pytest.importorskip("pandas")
    url = "https://example.test/input.jpg?signature=abc"
    frame = pd.DataFrame([{"id": "001", "query": "识别图片", "video_path": "__missing_video__", "attachment_url_domain": url}])
    source = tmp_path / f"input{suffix}"
    if suffix == ".csv":
        frame.to_csv(source, index=False)
    else:
        frame.to_excel(source, index=False)
    calls = mock_image_http(lambda request: httpx.Response(200, content=_image_bytes()))
    converted = convert_table(source, project_root=tmp_path)
    items, errors = parse_jsonl(converted.output_path.read_text(), "operation")
    assert not errors
    assert calls == []
    prepared = operation_media.prepare_operation_query_images(items[0], base_dir=tmp_path)
    assert prepared["source_data"]["attachment_url_domain"] == url
    assert prepared["source_data"]["query_images"] == [url]
    assert len(calls) == 1


def test_result_preview_reads_downloaded_image_without_network(tmp_path, mock_image_http, monkeypatch):
    calls = mock_image_http(lambda request: httpx.Response(200, content=_image_bytes()))
    item = operation_media.prepare_operation_query_images({"query_images": ["https://example.test/input.jpg"]}, base_dir=tmp_path)
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: {"items": [item]})
    response = server.api_export_item("image-task", 0, "query_image")
    assert response.media_type == "image/png"
    assert Path(response.path).is_file()
    assert len(calls) == 1
