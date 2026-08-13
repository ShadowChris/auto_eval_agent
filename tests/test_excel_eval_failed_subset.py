import json
from pathlib import Path

import pytest


pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from data.excel_eval_failed_subset import extract_failed_subset


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_extract_failed_subset_excludes_missing_video_and_keeps_source_order(
    tmp_path: Path,
):
    evaluation = tmp_path / "evaluation.xlsx"
    pd.DataFrame([
        {
            "item_id": "q3",
            "序号": "complex_001",
            "query": "第三题",
            "评估状态": "评估失败",
            "error": "TimeoutError: 单题评估超过 300 秒",
        },
        {
            "item_id": "q1",
            "序号": "simple_001",
            "query": "第一题",
            "评估状态": "评估失败",
            "error": "InternalServerError: provider busy",
        },
        {
            "item_id": "q2",
            "序号": "simple_002",
            "query": "第二题",
            "评估状态": "评估失败",
            "error": "ValueError: 视频文件不存在：data/q2.mp4",
        },
        {
            "item_id": "q4",
            "序号": "simple_004",
            "query": "第四题",
            "评估状态": "已完成",
            "error": "",
        },
    ]).to_excel(evaluation, sheet_name="逐题结果", index=False)
    dataset = tmp_path / "dataset.jsonl"
    _write_jsonl(dataset, [
        {"id": "q1", "query": "第一题", "自定义": 1},
        {"id": "q2", "query": "第二题", "自定义": 2},
        {"id": "q3", "query": "第三题", "自定义": 3},
        {"id": "q4", "query": "第四题", "自定义": 4},
    ])

    result = extract_failed_subset(evaluation, dataset)

    rows = [
        json.loads(line)
        for line in result.output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert result.output_path.name == "dataset_补充.jsonl"
    assert result.evaluation_rows == 4
    assert result.failed_rows == 3
    assert result.excluded_rows == 1
    assert result.selected_rows == 2
    assert [row["id"] for row in rows] == ["q1", "q3"]
    assert [row["序号"] for row in rows] == ["simple_001", "complex_001"]
    assert rows[0]["自定义"] == 1


def test_extract_failed_subset_writes_empty_dataset_when_all_failures_excluded(
    tmp_path: Path,
):
    evaluation = tmp_path / "evaluation.xlsx"
    pd.DataFrame([{
        "item_id": "q1",
        "query": "第一题",
        "评估状态": "评估失败",
        "error": "ValueError: 视频文件不存在：data/q1.mp4",
    }]).to_excel(evaluation, sheet_name="逐题结果", index=False)
    dataset = tmp_path / "dataset.jsonl"
    _write_jsonl(dataset, [{"id": "q1", "query": "第一题"}])

    result = extract_failed_subset(evaluation, dataset)

    assert result.selected_rows == 0
    assert result.output_path.read_text(encoding="utf-8") == ""


def test_extract_failed_subset_rejects_dataset_version_mismatch(tmp_path: Path):
    evaluation = tmp_path / "evaluation.xlsx"
    pd.DataFrame([{
        "item_id": "missing-id",
        "query": "第一题",
        "评估状态": "评估失败",
        "error": "TimeoutError",
    }]).to_excel(evaluation, sheet_name="逐题结果", index=False)
    dataset = tmp_path / "dataset.jsonl"
    _write_jsonl(dataset, [{"id": "q1", "query": "第一题"}])

    with pytest.raises(ValueError, match="原始 JSONL 中不存在"):
        extract_failed_subset(evaluation, dataset)
