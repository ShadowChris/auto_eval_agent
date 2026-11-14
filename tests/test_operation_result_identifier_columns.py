"""User-facing result sheets show id once, while internal identifiers stay intact."""
from copy import deepcopy
from io import BytesIO

from openpyxl import load_workbook

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.web import history
from auto_eval.web.operation_comparison_import import import_operation_comparison_file


def snapshot(task_id="base"):
    return dict(task_id=task_id, mode="operation", status="done", options={},
                items=[dict(id="q1", query="打开设置", video_path="video.mp4",
                            source_data=dict(id="q1",item_id="原始附加字段",query="打开设置",标签="保留"))],
                results=[dict(index=0,item_id="q1",correctness="ok",issue_types=[],rationale="完成")])


def test_user_results_hide_item_id_without_removing_original_or_runtime_fields():
    data = snapshot()
    original = deepcopy(data)
    exported_jsonl = history.jsonl_export_rows(data)
    sheets = history.export_rows(data)
    assert sheets["评估结果"][0]["id"] == "q1"
    assert "item_id" not in sheets["评估结果"][0]
    assert sheets["数据集明细"][0]["item_id"] == "原始附加字段"
    assert sheets["逐题结果"][0]["item_id"] == "q1"
    workbook = load_workbook(BytesIO(history.build_xlsx(data)), data_only=True)
    headers = [cell.value for cell in workbook["评估结果"][1]]
    assert headers.count("id") == 1 and "item_id" not in headers
    assert data == original and history.jsonl_export_rows(data) == exported_jsonl


def test_comparison_uses_id_only_headers_and_upload_still_recognizes_results():
    base = history.operation_comparison_batch(snapshot(), include_source_data=True)
    target = import_operation_comparison_file("target.xlsx", history.build_xlsx(snapshot("target")))
    assert target["summary"]["valid_count"] == 1 and target["rows"][0]["id"] == "q1"
    assert base["rows"][0]["export"]["item_id"] == "q1"
    payload = compare_operation_batches([base,target], baseline_task_id="base", include_union_rows=True)
    workbook = load_workbook(BytesIO(history.build_operation_comparison_xlsx(payload)), data_only=True)
    headers = [cell.value for cell in workbook["逐题横向对比"][2]]
    assert headers.count("id") >= 2 and "item_id" not in headers
