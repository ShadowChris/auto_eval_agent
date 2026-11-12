"""Import-preview metadata is read-only and uses parsed values, including zero."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from auto_eval.web.parse_input import parse_jsonl


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/auto_eval/web/static"


def test_optional_time_display_preserves_zero_decimals_and_defaults():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript display test")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    helper = "function formatOptionalTaskTime" + js.split(
        "function formatOptionalTaskTime", 1
    )[1].split("\n    function ", 1)[0]
    script = helper + """
    const values = [
        formatOptionalTaskTime(0, '开始'),
        formatOptionalTaskTime(12.75, '结束'),
        formatOptionalTaskTime(null, '开始'),
        formatOptionalTaskTime(undefined, '结束'),
        formatOptionalTaskTime(NaN, '开始'),
    ];
    process.stdout.write(JSON.stringify(values));
    """
    result = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == [
        "0 秒", "12.75 秒", "未设置（使用默认开始时间）",
        "未设置（使用默认结束时间）", "未设置（使用默认开始时间）",
    ]


def test_optional_fields_are_bound_to_parsed_card_values():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert 'class="hint op-optional-fields-help"' in html
    assert 'class="hint op-import-fields"' in html
    assert "formatOptionalTaskTime(entry.item.taskStartTime, '开始')" in html
    assert "formatOptionalTaskTime(entry.item.taskEndTime, '结束')" in html
    assert "formatAttachmentUrls(entry.item)" in html
    assert "域名地址优先" in html
    assert "源数据第 {{ entry.item.sourceLine }} 行" not in html
    assert "taskStartTime: item.task_start_time ?? null" in js
    assert "taskEndTime: item.task_end_time ?? null" in js
    assert "if (Number.isFinite(it.taskStartTime)) item.task_start_time = it.taskStartTime" in js
    assert ".op-import-fields { display: flex; flex-wrap: wrap;" in css


def test_attachment_urls_display_reports_primary_and_fallback_addresses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript display test")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    helper = "function formatAttachmentUrls" + js.split(
        "function formatAttachmentUrls", 1
    )[1].split("\n    function ", 1)[0]
    script = helper + """
    const values = [
        formatAttachmentUrls({attachmentPath: 'ignored.webp'}),
        formatAttachmentUrls({attachmentUrlDomain: 'https://example.test/a.webp', attachmentUrlIp: 'http://10.0.0.2/a.webp'}),
        formatAttachmentUrls({attachmentUrlDomain: '', attachmentUrlIp: 'http://10.0.0.2/a.webp'}),
    ];
    process.stdout.write(JSON.stringify(values));
    """
    result = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == [
        "未提供附件图片链接",
        "attachment_url_domain：https://example.test/a.webp；attachment_url_ip：http://10.0.0.2/a.webp（同一张图片，域名优先、IP 备用）",
        "attachment_url_domain：未提供；attachment_url_ip：http://10.0.0.2/a.webp（同一张图片，域名优先、IP 备用）",
    ]


def test_imported_times_and_queries_stay_aligned_with_source_rows():
    records = [
        {"id": "first", "query": "打开设置", "video_path": "one.mp4", "task_start_time": 0, "task_end_time": 12.75},
        {"id": "second", "query": "打开相机", "video_path": "two.mp4", "task_start_time": None},
    ]
    content = "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
    items, errors = parse_jsonl(content, "operation")
    assert errors == []
    assert items[0]["query"] == "打开设置"
    assert items[0]["task_start_time"] == 0
    assert items[0]["task_end_time"] == 12.75
    assert items[0]["source_line"] == 1
    assert items[1]["query"] == "打开相机"
    assert "task_start_time" not in items[1]
    assert "task_end_time" not in items[1]
    assert items[1]["source_line"] == 2


def test_web_preserves_fallback_urls_on_submit_and_clears_them_on_image_removal():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript display test")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'item.attachment_url_domain = it.attachmentUrlDomain || ""' in js
    assert 'item.attachment_url_ip = it.attachmentUrlIp || ""' in js
    assert 'attachmentUrlIp: String(item.attachment_url_ip' in js
    assert 'item.attachmentPath ? [item.attachmentPath]' not in js
    helper = "function removeQueryImage" + js.split("function removeQueryImage", 1)[1].split("\n    function ", 1)[0]
    script = """
    const assert = require('assert').strict;
    const source = {attachment_url_domain:'https://test/a', attachment_url_ip:'http://10.0.0.2/a'};
    const opItems = {value:[{queryImages:['https://test/a'], attachmentUrlDomain:source.attachment_url_domain,
      attachmentUrlIp:source.attachment_url_ip, sourceData:source}]};
    const releaseQueryImagePreviews = () => {};
    """ + helper + """
    removeQueryImage(0);
    assert.deepEqual(opItems.value[0].queryImages, []);
    assert.equal(opItems.value[0].attachmentUrlDomain, '');
    assert.equal(opItems.value[0].attachmentUrlIp, '');
    assert.equal(opItems.value[0].sourceData.attachment_url_domain, 'https://test/a');
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_empty_optional_time_cells_do_not_drop_table_rows():
    record = {
        "id": "empty-times",
        "query": "打开设置",
        "video_path": "video.mp4",
        "task_start_time": "",
        "task_end_time": "",
    }
    items, errors = parse_jsonl(json.dumps(record, ensure_ascii=False), "operation")
    assert errors == []
    assert len(items) == 1
    assert "task_start_time" not in items[0]
    assert "task_end_time" not in items[0]
