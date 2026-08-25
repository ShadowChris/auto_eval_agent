import io

import pytest
from fastapi.testclient import TestClient


pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from auto_eval.web.server import app


def test_import_xlsx_maps_operation_fields_and_preserves_source_columns():
    workbook = io.BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame([{"note": "不应读取"}]).to_excel(
            writer,
            sheet_name="设备原始",
            index=False,
        )
        pd.DataFrame([{
            "id": "raw-id",
            "index": "simple_001",
            "序号": "simple_001",
            "query": "打开设置",
            "开始时间节点": "2026-08-25 10:00:00",
            "位置信息": "杭州市滨江区",
            "回复内容": "已打开设置",
            "video_path": "",
            "分享链接": "https://example.test/case/1",
            "自定义列": "原始值",
        }]).to_excel(writer, sheet_name="合并数据", index=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/operation/import-table",
            files={
                "file": (
                    "0824-result.xlsx",
                    workbook.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["sheet"] == "合并数据"
    assert payload["summary"]["source_rows"] == 1
    assert payload["summary"]["imported_rows"] == 1
    assert payload["summary"]["missing_video_rows"] == 1
    item = payload["items"][0]
    assert item["id"] == "0824-result_simple_001"
    assert item["query"] == "打开设置"
    assert item["context"] == "交互发生时间：2026-08-25 10:00:00；交互发生位置：杭州市滨江区"
    assert item["answer"] == "已打开设置"
    assert item["video_path"] == "__missing_video__"
    assert item["source_data"]["id_1"] == "raw-id"
    assert item["source_data"]["分享链接"] == "https://example.test/case/1"
    assert item["source_data"]["自定义列"] == "原始值"


def test_import_csv_rejects_missing_identifier_columns():
    content = "query,video_path\n打开设置,data/video.mp4\n".encode("utf-8")
    with TestClient(app) as client:
        response = client.post(
            "/api/operation/import-table",
            files={"file": ("cases.csv", content, "text/csv")},
        )

    assert response.status_code == 422
    assert "index 或 序号" in response.json()["detail"]
