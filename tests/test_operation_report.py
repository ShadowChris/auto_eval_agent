from __future__ import annotations

import json
import re
from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.analysis.operation_report import (
    build_comparison_report, report_case, safe_report_url,
)
from auto_eval.report.operation import build_operation_report_html
from auto_eval.web import server
from auto_eval.web.history import operation_comparison_batch
from auto_eval.web.operation_comparison_import import import_operation_comparison_file


def snapshot(task_id: str = "report-control") -> dict:
    items, results = [], []
    statuses = ["ok", "nok", "no_support", "others"]
    issues = ["回复语义重复", "任务结果错误", "缺少前置条件", "未展示可验证结果"]
    for index in range(60):
        items.append({
            "id": f"item_{index}",
            "query": ["关闭定位", "打开蓝牙", "设置字体大小"][index % 3],
            "context": "交互发生位置：实验室",
            "source_data": {
                "index": f"simple_{index + 1:03d}",
                "sessionid": f"session_{index}",
                "video_url_domain": f"https://video.example.test/{task_id}/{index}.mp4",
                "video_url_ip": f"http://192.0.2.10/{task_id}/{index}.mp4",
                "unrelated_secret": "DO_NOT_EXPORT",
            },
        })
        results.append({
            "index": index,
            "correctness": statuses[index % 4],
            "issue_types": [issues[index % 4]],
            "rationale": f"第 {index + 1} 条判定理由",
            "answer": "评估示例回答",
            "rubric": {"操作完成度": 3, "步骤正确性": 4},
            "rubric_reasons": {"操作完成度": "示例依据"},
            "model_raw_output": "DO_NOT_EXPORT_RAW",
            "frames": ["data:image/png;base64,SECRET_FRAME"],
            "duration_s": 3.4,
        })
    return {
        "task_id": task_id, "dataset_name": f"{task_id}.jsonl",
        "mode": "operation", "status": "done", "options": {},
        "items": items, "results": results, "summary": {},
    }


def decode_report(document: bytes) -> dict:
    match = re.search(
        r'<script id="operation-report-data" type="application/json">(.*?)</script>',
        document.decode("utf-8"), re.DOTALL,
    )
    assert match
    return json.loads(match.group(1))


def test_single_report_uses_latest_results_and_keeps_both_video_sites() -> None:
    data = snapshot()
    data["results"][0]["error"] = "evaluation failed"
    data["results"].pop()
    report = server._single_operation_report(data)
    assert report["statistics"]["valid_count"] == 58
    assert report["statistics"]["failed_count"] == 1
    assert report["statistics"]["pending_count"] == 1
    assert len(report["cases"]) == 58
    row = report["cases"][0]
    assert row["index"] == "simple_002"
    assert row["video_url_domain"].endswith("/1.mp4")
    assert row["video_url_ip"].endswith("/1.mp4")
    assert row["rubric"]["操作完成度"] == 3
    assert row["sessionid"] == "session_1"
    serialized = json.dumps(report)
    assert "DO_NOT_EXPORT" not in serialized
    assert "SECRET_FRAME" not in serialized
    # 重跑成功后原位覆盖：分布和 Case 同时修正，不沿用旧 summary。
    before = report["statistics"]["ok_count"]
    data["results"][0] = {"index": 0, "correctness": "ok", "issue_types": []}
    updated = server._single_operation_report(data)
    assert updated["statistics"]["ok_count"] == before + 1
    assert updated["statistics"]["failed_count"] == 0


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "data:text/html,hello", "//evil.test",
    "https://user:pass@example.test/v.mp4", "https://example.test\\evil",
    "https://example.test/\nnext", "https://example.test:bad/file",
    None, "NA", "",
])
def test_report_rejects_unsafe_video_links(value) -> None:
    assert safe_report_url(value) == ""


@pytest.mark.parametrize("format", ["jsonl", "csv", "xlsx"])
def test_import_comparison_preserves_video_sites(format: str) -> None:
    record = {
        "index": "simple_001", "query": "打开蓝牙", "correctness": "nok",
        "issue_types": "任务结果错误",
        "video_url_domain": "https://video.example.test/one",
        "video_url_ip": "http://192.0.2.2:8000/one",
    }
    if format == "jsonl":
        content = json.dumps(record).encode()
    elif format == "csv":
        content = pd.DataFrame([record]).to_csv(index=False).encode("utf-8")
    else:
        output = BytesIO()
        pd.DataFrame([record]).to_excel(output, index=False)
        content = output.getvalue()
    source = import_operation_comparison_file(f"input.{format}", content)
    row = report_case(source["rows"][0])
    assert row["video_url_domain"] == record["video_url_domain"]
    assert row["video_url_ip"] == record["video_url_ip"]


def test_report_pairs_reuse_index_matching_and_valid_intersection() -> None:
    left = snapshot("control")
    right = snapshot("experiment")
    third = snapshot("third")
    # 重复 Query 仍按 index 对齐，顺序不同不串录屏。
    right["items"].reverse()
    for i, item in enumerate(right["items"]):
        right["results"][i]["query"] = item["query"]
    right["results"][0]["error"] = "timeout"
    third["results"][1]["error"] = "timeout"
    batches = [operation_comparison_batch(d) for d in (left, right, third)]
    comparison = compare_operation_batches(batches, baseline_task_id="control")
    report = build_comparison_report(batches, comparison)
    assert report["all_groups_common_valid_count"] == 58
    assert [p["valid_pair_count"] for p in report["pairs"]] == [59, 59]
    groups = {g["task_id"]: g for g in report["groups"]}
    for pair in report["pairs"]:
        assert len(pair["matches"]) == pair["valid_pair_count"]
        for lpos, rpos in pair["matches"]:
            lrow = groups["control"]["cases"][lpos]
            rrow = groups[pair["target_task_id"]]["cases"][rpos]
            assert lrow["index"] == rrow["index"]
            assert lrow["valid"] and rrow["valid"]
            assert "/control/" in lrow["video_url_domain"]
            assert f'/{pair["target_task_id"]}/' in rrow["video_url_domain"]


def test_html_exports_are_self_contained_and_escape_dataset_text(tmp_path) -> None:
    data = snapshot()
    payload = server._single_operation_report(data)
    hostile = '</script><script>globalThis.INJECTED=1</script><img src=x onerror=alert(1)>'
    payload["cases"][0]["query"] = hostile
    payload["dataset_name"] = hostile
    html = build_operation_report_html(payload)
    assert decode_report(html)["cases"][0]["query"] == hostile
    assert hostile.encode() not in html
    assert b'<script src=' not in html
    assert b'<link rel="stylesheet"' not in html
    assert b"connect-src 'none'" in html
    (tmp_path / "single.html").write_bytes(html)


def test_report_api_and_html_export_content_type(monkeypatch, tmp_path) -> None:
    current = snapshot()
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: current)
    client = TestClient(server.app)
    response = client.get("/api/eval/report-control/report")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    document = client.get("/api/eval/report-control/export?format=html")
    assert document.status_code == 200
    assert document.headers["content-type"].startswith("text/html")
    assert ".html" in document.headers["content-disposition"]
    assert ".csv" not in document.headers["content-disposition"]
    assert decode_report(document.content)["statistics"] == response.json()["statistics"]
    (tmp_path / "single.html").write_bytes(document.content)
    current["mode"] = "single"
    assert client.get("/api/eval/report-control/report").status_code == 409
    current["mode"] = "operation"
    current["options"]["operation_layout"] = "multi_group"
    assert client.get("/api/eval/report-control/export?format=html").status_code == 409
    monkeypatch.setattr(server, "load_snapshot", lambda _: None)
    assert client.get("/api/eval/missing/report").status_code == 404


def test_comparison_html_and_live_report_share_statistics(monkeypatch, tmp_path) -> None:
    source = snapshot("control")
    experiment = snapshot("experiment")
    experiment["results"][1]["correctness"] = "ok"
    experiment["results"][1]["issue_types"] = []
    experiment["results"][0]["issue_types"] = ["内部过程信息泄露"]
    snapshots = {"control": source, "experiment": experiment}
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", snapshots.get)
    client = TestClient(server.app)
    body = {
        "control_source_id": "control",
        "sources": [
            {"source_id": key, "source_type": "history", "task_id": key}
            for key in snapshots
        ],
    }
    live = client.post("/api/operation/comparison/analyze", json=body)
    assert live.status_code == 200
    document = client.post("/api/operation/comparison/export?format=html", json=body)
    assert document.status_code == 200
    assert document.headers["content-type"].startswith("text/html")
    assert ".html" in document.headers["content-disposition"]
    exported = decode_report(document.content)
    assert exported["pairs"] == live.json()["report"]["pairs"]
    assert exported["groups"] == live.json()["report"]["groups"]
    (tmp_path / "comparison.html").write_bytes(document.content)
    assert client.post("/api/operation/comparison/export?format=csv", json=body).status_code == 422


def test_web_includes_versioned_shared_report_assets() -> None:
    html = server.index().body.decode()
    version = server._static_asset_version()
    assert f"/report-assets/operation_report.js?v={version}" in html
    assert f"/report-assets/operation_report.css?v={version}" in html
    assert '导出 HTML 报告' in html
    assert '<operation-report' in html
