"""附件域名/IP 为一张图片的备选地址，导入只映射，准备阶段才下载。"""
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from auto_eval.query_images import attachment_image_fields, attachment_image_urls
from auto_eval.table_dataset import convert_table
from auto_eval.web import history, operation_media, server
from auto_eval.web.parse_input import parse_jsonl


DOMAIN = "https://images.example.test/input.webp?signature=secret"
IP = "http://10.0.0.2/input.webp"


def _raw(**fields):
    return {"id": "image-1", "query": "识别图片", "video_path": "data/video.mp4", **fields}


@pytest.fixture
def image_http(monkeypatch):
    body = BytesIO()
    Image.new("RGB", (32, 24), "blue").save(body, format="WEBP")
    calls = []

    def configure(handler):
        @contextmanager
        def stream(method, url, **kwargs):
            calls.append(url)
            with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, **kwargs) as client:
                with client.stream(method, url) as response:
                    yield response
        monkeypatch.setattr(operation_media.httpx, "stream", stream)
        return body.getvalue(), calls
    return configure


@pytest.mark.parametrize("nested", [False, True])
def test_jsonl_and_single_api_map_both_urls_to_one_image(nested):
    fields = {"attachment_url_domain": DOMAIN, "attachment_url_ip": IP, "attachment_path": ["ignored.png"]}
    raw = _raw(**({"metadata": fields} if nested else fields))
    original = deepcopy(raw)
    items, errors = parse_jsonl(json.dumps(raw), "operation")
    assert errors == []
    item = server._normalize_single_operation_item(raw)
    assert item == items[0]
    assert item["query_images"] == [DOMAIN]
    assert item["attachment_url_domain"] == DOMAIN
    assert item["attachment_url_ip"] == IP
    assert item["source_data"]["attachment_path"] == ["ignored.png"]
    if nested:
        assert item["source_data"]["metadata"] == fields
    assert raw == original
    preview = server._append_new_item_preview(item, 0, "image-1")
    assert preview["query_images"] == [DOMAIN]
    assert preview["attachment_url_domain"] == DOMAIN
    assert preview["attachment_url_ip"] == IP


@pytest.mark.parametrize("empty", [None, "", " \t ", float("nan")])
def test_empty_urls_mean_no_image_even_when_legacy_attachment_is_present(empty, tmp_path, monkeypatch):
    raw = _raw(metadata={"attachment_url_domain": empty, "attachment_url_ip": empty}, attachment_path=["ignored.webp"])
    items, errors = parse_jsonl(json.dumps(raw), "operation")
    assert not errors
    assert "query_images" not in items[0]
    monkeypatch.setattr(operation_media.httpx, "stream", lambda *_args, **_kwargs: pytest.fail("无图不能下载"))
    assert "query_images" not in operation_media.prepare_operation_query_images(items[0], base_dir=tmp_path)


def test_top_level_empty_values_override_metadata_and_source_data():
    raw = _raw(attachment_url_domain="", attachment_url_ip=None,
               metadata={"attachment_url_domain": DOMAIN, "attachment_url_ip": IP},
               source_data={"attachment_url_domain": DOMAIN})
    assert attachment_image_fields(raw) == {"attachment_url_domain": "", "attachment_url_ip": ""}
    assert attachment_image_urls(raw) == []


@pytest.mark.parametrize("value", [[], {"url": DOMAIN}, 123, True])
def test_malformed_new_url_fields_do_not_silently_become_no_image(value):
    items, errors = parse_jsonl(json.dumps(_raw(metadata={"attachment_url_domain": value})), "operation")
    assert items == []
    assert "attachment_url_domain" in errors[0]


def test_domain_success_does_not_download_ip(tmp_path, image_http):
    body, calls = image_http(lambda _: httpx.Response(200, content=body))
    item = operation_media.prepare_operation_query_images(_raw(attachment_url_domain=DOMAIN, attachment_url_ip=IP), base_dir=tmp_path)
    assert calls == [DOMAIN]
    assert len(item["query_images"]) == 1
    assert Path(item["query_images"][0]).read_bytes() == body


@pytest.mark.parametrize("failure", ["http", "timeout", "html", "unsupported"])
def test_domain_failure_falls_back_to_ip_and_rerun_reuses_local_copy(tmp_path, image_http, failure):
    def handler(request):
        if request.url.host == "images.example.test":
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if failure == "html":
                return httpx.Response(200, content=b"<html>error</html>")
            if failure == "unsupported":
                gif = BytesIO()
                Image.new("RGB", (16, 16)).save(gif, format="GIF")
                return httpx.Response(200, content=gif.getvalue())
            return httpx.Response(403)
        return httpx.Response(200, content=body)
    body, calls = image_http(handler)
    raw = _raw(metadata={"attachment_url_domain": DOMAIN, "attachment_url_ip": IP, "label": "保留"})
    item = server._normalize_single_operation_item(raw)
    before = deepcopy(item)
    prepared = operation_media.prepare_operation_query_images(item, base_dir=tmp_path)
    assert calls == [DOMAIN, IP]
    assert len(prepared["query_images"]) == 1
    assert Path(prepared["query_images"][0]).read_bytes() == body
    assert item == before
    assert prepared["source_data"]["metadata"] == raw["metadata"]
    assert len(list((tmp_path / "runs/uploads/query_images/remote").iterdir())) == 1
    again = operation_media.prepare_operation_query_images(prepared, base_dir=tmp_path)
    assert again["query_images"] == prepared["query_images"]
    assert len(calls) == 2
    exported = history.jsonl_export_rows({"mode": "operation", "items": [prepared], "results": []})[0]
    assert exported["metadata"] == raw["metadata"]
    assert exported["attachment_url_domain"] == DOMAIN
    assert exported["attachment_url_ip"] == IP


def test_both_urls_fail_with_error_and_no_partial_images(tmp_path, image_http):
    _, calls = image_http(lambda _: httpx.Response(404))
    with pytest.raises(ValueError) as error:
        operation_media.prepare_operation_query_images(_raw(attachment_url_domain=DOMAIN, attachment_url_ip=IP), base_dir=tmp_path)
    assert calls == [DOMAIN, IP]
    assert "attachment_url_domain" in str(error.value)
    assert "attachment_url_ip" in str(error.value)
    assert "HTTP 404" in str(error.value)
    assert "secret" not in str(error.value)
    assert not list((tmp_path / "runs/uploads/query_images/remote").iterdir())


def test_equal_urls_are_only_tried_once(tmp_path, image_http):
    _, calls = image_http(lambda _: httpx.Response(404))
    with pytest.raises(ValueError, match="HTTP 404"):
        operation_media.prepare_operation_query_images(_raw(attachment_url_domain=DOMAIN, attachment_url_ip=DOMAIN), base_dir=tmp_path)
    assert calls == [DOMAIN]


def test_ip_only_or_invalid_domain_can_use_ip(tmp_path, image_http):
    body, calls = image_http(lambda _: httpx.Response(200, content=body))
    for domain in (None, " ", "not-a-url"):
        prepared = operation_media.prepare_operation_query_images(_raw(attachment_url_domain=domain, attachment_url_ip=IP), base_dir=tmp_path)
        assert len(prepared["query_images"]) == 1
    assert calls == [IP, IP, IP]


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_table_urls_keep_both_original_columns_and_defer_download(tmp_path, image_http, suffix):
    pd = pytest.importorskip("pandas")
    source = tmp_path / f"input{suffix}"
    frame = pd.DataFrame([
        {"id": "both", "query": "q", "video_path": "v.mp4", "attachment_url_domain": DOMAIN, "attachment_url_ip": IP, "attachment_path": "old.webp"},
        {"id": "ip", "query": "q", "video_path": "v.mp4", "attachment_url_domain": " ", "attachment_url_ip": IP},
        {"id": "empty", "query": "q", "video_path": "v.mp4", "attachment_url_domain": "", "attachment_url_ip": None, "attachment_path": "ignored.png"},
    ])
    if suffix == ".csv":
        frame.to_csv(source, index=False)
    else:
        frame.to_excel(source, index=False)
    _, calls = image_http(lambda _: pytest.fail("导入不能联网"))
    converted = convert_table(source, project_root=tmp_path)
    assert converted.rows[0]["query_images"] == [DOMAIN]
    assert converted.rows[1]["query_images"] == [IP]
    assert "query_images" not in converted.rows[2]
    assert converted.rows[0]["attachment_url_domain"] == DOMAIN
    assert converted.rows[0]["attachment_url_ip"] == IP
    assert converted.rows[0]["attachment_path"] == "old.webp"
    assert "attachment_url_domain_1" not in converted.rows[0]
    items, errors = parse_jsonl(converted.output_path.read_text(), "operation")
    assert not errors
    assert items[0]["attachment_url_ip"] == IP
    assert "query_images" not in items[2]
    assert not calls


def test_explicit_local_images_still_take_priority(tmp_path, image_http):
    path = tmp_path / "input.webp"
    Image.new("RGB", (20, 20)).save(path)
    _, calls = image_http(lambda _: pytest.fail("显式本地图片无需读取附件地址"))
    raw = _raw(query_images=[str(path)], attachment_url_domain=DOMAIN, attachment_url_ip=IP)
    assert operation_media.prepare_operation_query_images(raw, base_dir=tmp_path)["query_images"] == [str(path)]
    assert not calls
