from __future__ import annotations

import json
from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from auto_eval.web import server
from auto_eval.web.history import _OPERATION_USER_RESULT_HEADERS
from auto_eval.web.operation_comparison_import import (
    import_operation_comparison_file,
    validate_uploaded_comparison_source,
)
from auto_eval.web.server import (
    OperationComparisonAnalyzeReq,
    OperationComparisonSourceReq,
)


def test_import_comparison_jsonl_reads_nested_evaluation() -> None:
    content = "\n".join([
        json.dumps({
            "id": "case_1",
            "index": "simple_001",
            "case_id": "c1",
            "query": "打开蓝牙",
            "context": "测试环境",
            "evaluation": {
                "correctness": "ok",
                "issue_types": [],
                "need_review_cate": [],
                "rationale": "已经打开",
            },
        }, ensure_ascii=False),
        json.dumps({
            "id": "case_2",
            "index": "simple_002",
            "case_id": "c2",
            "query": "关闭定位",
            "evaluation": {
                "correctness": "nok",
                "issue_types": ["应执行目标未执行"],
                "need_review_cate": [],
                "rationale": "定位未关闭",
            },
        }, ensure_ascii=False),
    ]).encode("utf-8")

    source = import_operation_comparison_file("结果.jsonl", content)

    assert source["summary"]["raw_count"] == 2
    assert source["summary"]["valid_count"] == 2
    assert source["task_id"] == source["source_id"]
    assert source["group_name"] == "结果.jsonl"
    assert source["mapping"]["id"] == "id"
    assert source["mapping"]["index"] == "index"
    assert source["rows"][0]["index"] == "simple_001"
    assert source["mapping"]["correctness"] == "evaluation.correctness"
    assert source["rows"][1]["result"]["issue_types"] == ["应执行目标未执行"]
    assert source["rows"][0]["export"]["context"] == "测试环境"


def test_import_comparison_excel_prefers_result_sheet() -> None:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([{"query": "错误工作表"}]).to_excel(
            writer,
            sheet_name="数据集明细",
            index=False,
        )
        pd.DataFrame([{
            "item_id": "i1",
            "index": "simple_001",
            "case_id": "c1",
            "query": "打开蓝牙",
            "correctness": "ok",
            "issue_types": "",
            "need_review_cate": "",
            "rationale": "已完成",
        }]).to_excel(writer, sheet_name="逐题结果", index=False)

    source = import_operation_comparison_file("结果.xlsx", output.getvalue())

    assert source["summary"]["sheet"] == "逐题结果"
    assert source["summary"]["valid_count"] == 1
    assert source["rows"][0]["query"] == "打开蓝牙"


def test_import_comparison_csv_maps_chinese_columns_and_warnings() -> None:
    content = (
        "id,index,case_id,操作意图,完成判定,问题类型,需复核类别,判定理由\n"
        "item_001,simple_001,c1,打开蓝牙,OK,路径冗余；重复操作,,已完成\n"
        "item_002,simple_002,c2,关闭定位,unknown,,,无有效判定\n"
    ).encode("utf-8-sig")

    with TestClient(server.app) as client:
        response = client.post(
            "/api/operation/comparison/import",
            files={"file": ("实验组.csv", content, "text/csv")},
        )

    assert response.status_code == 200
    source = response.json()
    assert source["mapping"]["query"] == "操作意图"
    assert source["summary"]["valid_count"] == 1
    assert source["summary"]["invalid_count"] == 1
    assert source["summary"]["warning_count"] == 1
    assert source["rows"][0]["result"]["issue_types"] == ["路径冗余", "重复操作"]


def test_import_comparison_warns_for_missing_and_duplicate_id() -> None:
    content = (
        "id,query,correctness,issue_types,need_review_cate,rationale\n"
        "same,打开蓝牙,ok,,,已完成\n"
        "same,关闭定位,nok,,,未完成\n"
        ",设置字号,ok,,,已完成\n"
        "unique,打开设置,ok,,,已完成\n"
    ).encode("utf-8-sig")

    source = import_operation_comparison_file("实验组.csv", content)

    messages = [warning["message"] for warning in source["warnings"]]
    assert any("id 不能为空" in message for message in messages)
    assert any("id=same 重复" in message for message in messages)
    assert source["summary"]["valid_count"] == 1
    assert source["summary"]["invalid_count"] == 3
    assert all(row["result"]["error"] for row in source["rows"][:3])


def test_import_comparison_rejects_file_without_valid_correctness() -> None:
    content = "id,query,correctness,issue_types,need_review_cate,rationale\nc1,打开蓝牙,partial,,,未完成\n".encode("utf-8")

    with pytest.raises(ValueError, match="correctness 必须是"):
        import_operation_comparison_file("旧标准.csv", content)


def _required_record(**overrides) -> dict:
    return {
        "id": "001", "query": "打开蓝牙", "correctness": "ok",
        "issue_types": "", "need_review_cate": "", "rationale": "蓝牙已打开",
        **overrides,
    }


@pytest.mark.parametrize("field", [
    "id", "query", "correctness", "issue_types", "rationale",
])
def test_import_comparison_requires_all_five_columns(field: str) -> None:
    record = _required_record()
    record.pop(field)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/operation/comparison/import",
            files={"file": ("missing.csv", pd.DataFrame([record]).to_csv(index=False).encode(), "text/csv")},
        )
    assert response.status_code == 422
    assert "缺少必需列" in response.json()["detail"]
    assert field in response.json()["detail"]


@pytest.mark.parametrize("format,nested", [
    ("csv", False), ("xlsx", False), ("jsonl", False), ("jsonl", True),
])
def test_import_legacy_results_without_review_column(format: str, nested: bool) -> None:
    record = _required_record()
    record.pop("need_review_cate")
    if nested:
        record = {
            "id": record.pop("id"), "query": record.pop("query"),
            "evaluation": record,
        }
    if format == "jsonl":
        content = json.dumps(record).encode()
    elif format == "csv":
        content = pd.DataFrame([record]).to_csv(index=False).encode()
    else:
        output = BytesIO()
        pd.DataFrame([record]).to_excel(output, sheet_name="评估结果", index=False)
        content = output.getvalue()

    source = import_operation_comparison_file(f"legacy.{format}", content)
    assert source["summary"]["valid_count"] == 1
    assert source["summary"]["warning_count"] == 0
    assert source["mapping"]["need_review_cate"] == ""
    assert source["rows"][0]["result"]["need_review_cate"] == []
    assert validate_uploaded_comparison_source(source)["rows"][0]["result"]["need_review_cate"] == []
    request = OperationComparisonAnalyzeReq(
        sources=[OperationComparisonSourceReq(**source), OperationComparisonSourceReq(**{
            **source, "source_id": "upload_legacy_other", "task_id": "upload_legacy_other",
        })], control_source_id=source["source_id"],
    )
    payload = server.api_operation_comparison_analyze(request)
    assert payload["all_groups_common_valid_count"] == 1
    assert payload["pairwise"][0]["review_category_rows"] == []


@pytest.mark.parametrize("field", ["id", "query", "correctness", "rationale"])
def test_import_invalid_values_are_retained_but_excluded_from_statistics(field: str) -> None:
    records = [_required_record(), _required_record(id="002", **({field: " "} if field != "id" else {}))]
    if field == "id":
        records[1]["id"] = " "
    source = import_operation_comparison_file(
        "invalid.csv", pd.DataFrame(records).to_csv(index=False).encode(),
    )
    assert source["summary"]["valid_count"] == 1
    assert source["summary"]["invalid_count"] == 1
    assert source["warnings"][0]["row"] == 3
    assert field in source["warnings"][0]["message"]
    assert source["rows"][0]["id"] == "001"  # CSV 文本 ID 的前导零不丢失。
    assert source["rows"][1]["result"]["error"]

    request = OperationComparisonAnalyzeReq(
        sources=[OperationComparisonSourceReq(**source), OperationComparisonSourceReq(**{
            **source, "source_id": "upload_another", "task_id": "upload_another",
        })], control_source_id=source["source_id"],
    )
    payload = server.api_operation_comparison_analyze(request)
    assert payload["all_groups_common_valid_count"] == 1


def test_uploaded_source_is_revalidated_before_analysis() -> None:
    source = import_operation_comparison_file(
        "results.jsonl", json.dumps(_required_record()).encode(),
    )
    source["rows"][0]["result"]["rationale"] = ""
    source["rows"][0]["export"]["rationale"] = ""
    with pytest.raises(ValueError, match="没有可参与对比的有效记录"):
        validate_uploaded_comparison_source(source)


def test_import_keeps_failed_native_rows_without_requiring_a_judgment() -> None:
    records = [_required_record(), _required_record(
        id="002", correctness="", rationale="", error="模型请求超时",
    )]
    source = import_operation_comparison_file(
        "results.jsonl", "\n".join(json.dumps(record) for record in records).encode(),
    )
    assert source["summary"]["valid_count"] == 1
    assert source["summary"]["invalid_count"] == 1
    assert source["rows"][1]["result"]["error"] == "模型请求超时"
    assert validate_uploaded_comparison_source(source)["source_type"] == "upload"


def _history_snapshot() -> dict:
    return {
        "task_id": "history_1",
        "dataset_name": "历史对照.jsonl",
        "mode": "operation",
        "status": "done",
        "options": {},
        "items": [{
            "id": "i1",
            "case_id": "c1",
            "query": "打开蓝牙",
            "source_data": {"index": "simple_001"},
        }],
        "results": [{"index": 0, "correctness": "nok", "issue_types": ["应执行目标未执行"]}],
        "summary": {},
    }


def test_comparison_analysis_mixes_history_and_upload(monkeypatch) -> None:
    imported = import_operation_comparison_file(
        "实验.jsonl",
        json.dumps({
            "id": "i1",
            "index": "simple_001",
            "case_id": "c1",
            "query": "打开蓝牙",
            "evaluation": {
                "correctness": "ok", "issue_types": [],
                "need_review_cate": [], "rationale": "已打开蓝牙",
            },
        }, ensure_ascii=False).encode("utf-8"),
    )
    monkeypatch.setattr(server, "get_live_task", lambda _task_id: None)
    monkeypatch.setattr(
        server,
        "load_snapshot",
        lambda task_id: _history_snapshot() if task_id == "history_1" else None,
    )
    request = OperationComparisonAnalyzeReq(
        sources=[
            OperationComparisonSourceReq(
                source_id="history_1",
                source_type="history",
                task_id="history_1",
                group_name="历史对照",
            ),
            OperationComparisonSourceReq(**imported),
        ],
        control_source_id="history_1",
    )

    payload = server.api_operation_comparison_analyze(request)

    assert payload["group_count"] == 2
    assert payload["all_groups_common_valid_count"] == 1
    assert payload["groups"][0]["group_label"] == "对照组"
    assert payload["groups"][1]["group_label"] == "实验组A"
    assert [group["source_type"] for group in payload["groups"]] == ["history", "upload"]
    assert [group["source_label"] for group in payload["report"]["groups"]] == ["原生评估", "上传结果"]
    assert payload["report"]["views"]["common_decidable"]["groups"][1]["source_type"] == "upload"
    assert payload["pairwise"][0]["to_ok_count"] == 1


def test_comparison_export_mixes_history_and_upload(monkeypatch) -> None:
    imported = import_operation_comparison_file(
        "实验.jsonl",
        json.dumps({
            "id": "i1",
            "index": "simple_001",
            "case_id": "c1",
            "query": "打开蓝牙",
            "sessionid": "session-a",
            "metadata": {"id": "business-001"},
            "attachment_path": "/data/query.png",
            "video_url_domain": "https://video.example/case-1.mp4",
            "录屏时长": 9.5,
            "evaluation": {
                "correctness": "ok",
                "issue_types": [],
                "need_review_cate": [],
                "rationale": "已完成",
                "rubric": {"操作完成度": 4, "步骤正确性": 3},
                "rubric_reasons": {"操作完成度": "已完成"},
            },
        }, ensure_ascii=False).encode("utf-8"),
    )
    monkeypatch.setattr(server, "get_live_task", lambda _task_id: None)
    monkeypatch.setattr(
        server,
        "load_snapshot",
        lambda task_id: _history_snapshot() if task_id == "history_1" else None,
    )
    request = OperationComparisonAnalyzeReq(
        sources=[
            OperationComparisonSourceReq(
                source_id="history_1",
                source_type="history",
                task_id="history_1",
                group_name="历史对照",
            ),
            OperationComparisonSourceReq(**imported),
        ],
        control_source_id="history_1",
    )

    response = server.api_operation_comparison_export(request)
    workbook = load_workbook(BytesIO(response.body), read_only=True)

    assert workbook.sheetnames == ["逐题横向对比", "数据集明细", "对比概览", "Issue Type对比", "需复核类别对比"]
    assert workbook["对比概览"]["L6"].value == "来源"
    assert workbook["对比概览"]["L7"].value == "原生评估"
    assert workbook["对比概览"]["L8"].value == "上传结果"
    detail = workbook["逐题横向对比"]
    header_rows = detail.iter_rows()
    group_headers = [cell.value for cell in next(header_rows)]
    field_headers = [cell.value for cell in next(header_rows)]
    value_row = [cell.value for cell in next(header_rows)]
    assert "实验组A：实验" in group_headers
    field_count = len(_OPERATION_USER_RESULT_HEADERS)
    assert tuple(field_headers[4:4 + field_count]) == _OPERATION_USER_RESULT_HEADERS
    assert tuple(field_headers[4 + field_count:4 + 2 * field_count]) == (
        _OPERATION_USER_RESULT_HEADERS
    )
    uploaded_values = dict(zip(
        _OPERATION_USER_RESULT_HEADERS,
        value_row[4 + field_count:4 + 2 * field_count],
    ))
    assert "是否挂卡" in uploaded_values
    assert "card_presence" not in uploaded_values
    assert uploaded_values["session_id"] == "session-a"
    assert uploaded_values["id"] == "i1"
    assert uploaded_values["metadata.id"] == "business-001"
    assert uploaded_values["attachment_path"] == "/data/query.png"
    assert uploaded_values["维度_操作完成度"] == 4
    assert uploaded_values["video_url_domain"] == "https://video.example/case-1.mp4"
    assert uploaded_values["录屏时长"] == 9.5


def test_comparison_module_ui_is_parallel_workspace() -> None:
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "任务类对比分析" in html
    assert "taskModule==='comparison'" in html
    assert "上传评估结果集" in html
    assert "加入对比分析" in html
    assert 'fetch("/api/operation/comparison/import"' in js
    assert 'fetch("/api/operation/comparison/analyze"' in js
    assert "/api/operation/comparison/export?format=" in js
