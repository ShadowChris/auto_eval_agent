import csv
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from scripts.csv_merge_by_device_group import (
    merge_csv_groups,
    parse_group_definition,
    parse_group_definitions,
)


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or []), list(reader)


def _write_xlsx(
    path: Path,
    rows: list[list[object]],
    *,
    second_sheet_rows: list[list[object]] | None = None,
) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "第一个工作表"
    for row in rows:
        worksheet.append(row)
    if second_sheet_rows is not None:
        second = workbook.create_sheet("不读取的工作表")
        for row in second_sheet_rows:
            second.append(row)
    workbook.save(path)


def test_parse_group_definition_accepts_chinese_punctuation():
    assert parse_group_definition("实验组：dev-1；dev-2;dev-3") == (
        "实验组",
        ("dev-1", "dev-2", "dev-3"),
    )


def test_merge_csvs_by_device_and_naturally_sort_sequence(tmp_path: Path):
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    _write_csv(
        source_dir / "run-1_DEV-A.csv",
        [
            {"序号": "simple_10", "query": "第十题"},
            {"序号": "simple_2", "query": "第二题"},
        ],
        ["序号", "query"],
    )
    _write_csv(
        source_dir / "run-2_DEV-A.csv",
        [{"序号": "simple_3", "query": "第三题", "extra": "A"}],
        ["序号", "query", "extra"],
    )
    _write_csv(
        source_dir / "run_DEV-B.csv",
        [{"序号": "simple_1", "query": "第一题"}],
        ["序号", "query"],
    )
    _write_csv(
        source_dir / "run_DEV-C.csv",
        [{"序号": "simple_4", "query": "对照题"}],
        ["序号", "query"],
    )
    _write_csv(
        source_dir / "说明.csv",
        [{"序号": "ignore", "query": "不参与"}],
        ["序号", "query"],
    )

    result = merge_csv_groups(
        source_dir,
        {
            "实验组": ("DEV-A", "DEV-B"),
            "对照组": ("DEV-C",),
        },
        output_name="0817测试",
        output_dir=tmp_path / "merged",
    )

    assert [group.group_name for group in result.groups] == ["实验组", "对照组"]
    assert [group.row_count for group in result.groups] == [4, 1]
    assert [path.name for path in result.unmatched_files] == ["说明.csv"]
    fields, rows = _read_csv(tmp_path / "merged" / "0817测试_实验组.csv")
    assert fields == ["序号", "query", "extra", "device_id"]
    assert [row["序号"] for row in rows] == [
        "simple_1",
        "simple_2",
        "simple_3",
        "simple_10",
    ]
    assert [row["device_id"] for row in rows] == [
        "DEV-B",
        "DEV-A",
        "DEV-A",
        "DEV-A",
    ]
    assert rows[2]["extra"] == "A"
    assert rows[0]["extra"] == ""


def test_rejects_device_assigned_to_multiple_groups():
    with pytest.raises(ValueError, match="同时属于"):
        parse_group_definitions([
            "实验组:DEV-A;DEV-B",
            "对照组:DEV-B;DEV-C",
        ])


def test_rejects_device_without_matching_csv(tmp_path: Path):
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    _write_csv(
        source_dir / "run_DEV-A.csv",
        [{"序号": "simple_1"}],
        ["序号"],
    )

    with pytest.raises(ValueError, match="DEV-B"):
        merge_csv_groups(
            source_dir,
            {"实验组": ("DEV-A", "DEV-B")},
            output_name="测试",
        )


def test_default_output_dir_is_input_merged_output_folder(tmp_path: Path):
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    _write_csv(
        source_dir / "run_DEV-A.csv",
        [{"序号": "simple_1", "query": "第一题"}],
        ["序号", "query"],
    )

    result = merge_csv_groups(
        source_dir,
        {"实验组": ("DEV-A",)},
        output_name="测试",
    )

    assert result.output_dir == source_dir / "merged_output"
    assert result.groups[0].output_path == (
        source_dir / "merged_output" / "测试_实验组.csv"
    )
    assert result.groups[0].output_path.is_file()


def test_merge_supports_csv_and_first_worksheet_of_xlsx(tmp_path: Path):
    source_dir = tmp_path / "tables"
    source_dir.mkdir()
    _write_csv(
        source_dir / "run_DEV-A.csv",
        [{"序号": "simple_2", "query": "CSV题"}],
        ["序号", "query"],
    )
    _write_xlsx(
        source_dir / "run_DEV-B.xlsx",
        [
            ["序号", "query", "xlsx_extra"],
            ["simple_1", "Excel题", "保留"],
        ],
        second_sheet_rows=[
            ["序号", "query"],
            ["simple_999", "不应读取"],
        ],
    )

    result = merge_csv_groups(
        source_dir,
        {"实验组": ("DEV-A", "DEV-B")},
        output_name="混合输入",
    )

    assert result.groups[0].row_count == 2
    assert {path.suffix for path in result.groups[0].source_files} == {".csv", ".xlsx"}
    fields, rows = _read_csv(
        source_dir / "merged_output" / "混合输入_实验组.csv"
    )
    assert fields == ["序号", "query", "xlsx_extra", "device_id"]
    assert [row["序号"] for row in rows] == ["simple_1", "simple_2"]
    assert rows[0]["query"] == "Excel题"
    assert rows[0]["xlsx_extra"] == "保留"
    assert rows[0]["device_id"] == "DEV-B"
