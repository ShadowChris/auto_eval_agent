"""单组与横向对比共用时间列，各组保留各自的原始录屏标注。"""
from io import BytesIO

from openpyxl import load_workbook
import pytest

from auto_eval.analysis.operation_comparison import compare_operation_batches
from auto_eval.web.history import (
    build_operation_comparison_xlsx,
    build_xlsx,
    operation_comparison_batch,
)
from auto_eval.web.operation_comparison_import import import_operation_comparison_file


def _snapshot(task_id, records):
    return {
        "task_id": task_id, "mode": "operation", "status": "done",
        "items": [
            {"id": record["id"], "query": record["query"], "source_data": record,
             "task_start_time": 7, "task_end_time": 100}
            for record in records
        ],
        "results": [{"index": index, "correctness": "ok", "rationale": "完成"}
                    for index, _ in enumerate(records)],
    }


@pytest.mark.parametrize("nested", [False, True])
def test_single_export_uses_original_time_bounds_including_zero(nested):
    times = {"task_start_time": 0, "task_end_time": 3.25}
    record = {"id": "a", "query": "打开设置", **({"metadata": times} if nested else times)}
    snapshot = _snapshot("single", [record])
    workbook = load_workbook(BytesIO(build_xlsx(snapshot)), data_only=True)
    sheet = workbook["评估结果"]
    headers = [cell.value for cell in sheet[1]]
    assert headers[-3:] == ["录屏时长", "task_start_time", "task_end_time"]
    assert sheet.cell(2, headers.index("task_start_time") + 1).value == 0
    assert sheet.cell(2, headers.index("task_end_time") + 1).value == 3.25


@pytest.mark.parametrize("uploaded", [False, True])
def test_comparison_exports_each_groups_time_bounds_after_id_alignment(uploaded):
    control = _snapshot("control", [
        {"id": "b", "query": "关闭定位", "task_start_time": 0, "task_end_time": 3.25},
        {"id": "a", "query": "打开蓝牙", "task_start_time": 1.5, "task_end_time": 4.5},
    ])
    experiment = _snapshot("experiment", [
        {"id": "a", "query": "打开蓝牙", "task_start_time": 2.5, "task_end_time": 6.5},
        {"id": "b", "query": "关闭定位", "task_start_time": 0.25, "task_end_time": 7.75},
    ])
    baseline = operation_comparison_batch(control, include_source_data=True)
    if uploaded:
        target = import_operation_comparison_file("experiment.xlsx", build_xlsx(experiment))
    else:
        target = operation_comparison_batch(experiment, include_source_data=True)
    payload = compare_operation_batches([baseline, target], baseline_task_id="control", include_union_rows=True)
    workbook = load_workbook(BytesIO(build_operation_comparison_xlsx(payload)), data_only=True)
    sheet = workbook["逐题横向对比"]
    headers = [cell.value for cell in sheet[2]]
    columns = [index + 1 for index, header in enumerate(headers)
               if header in {"task_start_time", "task_end_time"}]
    assert len(columns) == 4
    assert [sheet.cell(3, column).value for column in columns] == [0, 3.25, 0.25, 7.75]
    assert [sheet.cell(4, column).value for column in columns] == [1.5, 4.5, 2.5, 6.5]
