import json
from pathlib import Path

import pytest


pd = pytest.importorskip("pandas")

from scripts.excel_to_jsonl_dataset import convert_table, main


def test_convert_csv_to_ordered_operation_jsonl(tmp_path: Path):
    video = tmp_path / "videos" / "simple_001" / "record.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([
        {
            "序号": "simple_001",
            "query": "打开设置",
            "开始时间节点": "2026-08-05 10:00:00",
            "文件路径": "simple_001",
            "原文件名": "record.mp4",
            "回复内容": "已打开设置",
            "分享链接": "https://example.test/1",
        },
        {
            "序号": "simple_002",
            "query": "关闭设置",
            "开始时间节点": "2026-08-05 10:01:00",
            "文件路径": "simple_002",
            "原文件名": "missing.mp4",
            "回复内容": "",
            "分享链接": "https://example.test/2",
        },
    ]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(
        source,
        input_prefix="V1_录屏0805",
        video_prefix=str(tmp_path / "videos"),
        current_location="浙江省杭州市滨江区",
        project_root=tmp_path,
    )

    exported = [
        json.loads(line)
        for line in result.output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in exported] == [
        "V1_录屏0805_simple_001",
        "V1_录屏0805_simple_002",
    ]
    assert [row["序号"] for row in exported] == ["simple_001", "simple_002"]
    assert exported[0]["query"] == "打开设置"
    assert exported[0]["answer"] == "已打开设置"
    assert exported[0]["context"] == (
        "交互发生时间：2026-08-05 10:00:00；"
        "交互发生位置：浙江省杭州市滨江区"
    )
    assert exported[0]["video_path"].endswith("videos/simple_001/record.mp4")
    assert exported[0]["分享链接"] == "https://example.test/1"
    assert exported[0]["开始时间节点"] == "2026-08-05 10:00:00"
    assert exported[0]["回复内容"] == "已打开设置"
    assert exported[0]["文件路径"] == "simple_001"
    assert exported[0]["原文件名"] == "record.mp4"
    assert list(exported[0]) == [
        "id",
        "序号",
        "query",
        "context",
        "answer",
        "video_path",
        "开始时间节点",
        "文件路径",
        "原文件名",
        "回复内容",
        "分享链接",
    ]
    assert "answer" not in exported[1]
    assert exported[1]["回复内容"] == ""
    assert result.missing_video_ids == ["V1_录屏0805_simple_002"]


def test_video_directory_is_optional_and_similarly_named_columns_are_ignored(
    tmp_path: Path,
):
    video = tmp_path / "videos" / "record.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "序号": "simple_001",
        "query": "打开设置",
        "文件路径": "",
        "文件路径1": "这是备注，不参与路径拼接",
        "原文件名": "record.mp4",
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(
        source,
        input_prefix="V1",
        video_prefix=str(tmp_path / "videos"),
        project_root=tmp_path,
    )

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["video_path"] == str(video)
    assert exported["文件路径1"] == "这是备注，不参与路径拼接"
    assert result.missing_video_ids == []
    assert result.warnings == []


def test_existing_video_path_column_takes_priority_over_filename_mapping(
    tmp_path: Path,
):
    direct_video = tmp_path / "direct" / "actual.mp4"
    direct_video.parent.mkdir(parents=True)
    direct_video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "序号": "simple_001",
        "query": "打开设置",
        "video_path": str(direct_video),
        "文件路径": "wrong_directory",
        "原文件名": "wrong_name.mp4",
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(
        source,
        input_prefix="V1",
        video_prefix=str(tmp_path / "unused_prefix"),
        project_root=tmp_path,
    )

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["video_path"] == str(direct_video)
    assert result.missing_video_ids == []
    assert result.warnings == []


def test_explicit_standard_columns_take_priority_and_cli_prints_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    video = tmp_path / "direct.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    output = tmp_path / "cases.jsonl"
    pd.DataFrame([{
        "序号": "simple_001",
        "query": "打开设置",
        "context": "原表上下文",
        "answer": "原表回答",
        "回复内容": "不应覆盖原表回答",
        "task_start_time": 1,
        "开始时间": 9,
        "task_end_time": 2,
        "结束时间": 10,
        "video_path": str(video),
        "文件路径": "wrong_directory",
        "原文件名": "wrong_name.mp4",
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    exit_code = main([
        str(source),
        "--input-prefix",
        "V1",
        "--output",
        str(output),
    ])

    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert exported["context"] == "原表上下文"
    assert exported["answer"] == "原表回答"
    assert exported["task_start_time"] == 1.0
    assert exported["task_end_time"] == 2.0
    assert exported["video_path"] == str(video)
    assert exported["回复内容"] == "不应覆盖原表回答"
    assert exported["开始时间"] == "9"
    assert exported["结束时间"] == "10"
    assert exported["文件路径"] == "wrong_directory"
    assert exported["原文件名"] == "wrong_name.mp4"
    stdout = capsys.readouterr().out
    assert "警告：输入表包含可自动生成的同名标准字段" in stdout
    assert (
        "context、answer、task_start_time、task_end_time、video_path"
        in stdout
    )


def test_empty_value_in_existing_video_path_column_does_not_fall_back_to_filename(
    tmp_path: Path,
):
    fallback_video = tmp_path / "videos" / "simple_001" / "record.mp4"
    fallback_video.parent.mkdir(parents=True)
    fallback_video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "序号": "simple_001",
        "query": "打开设置",
        "video_path": "",
        "文件路径": "simple_001",
        "原文件名": "record.mp4",
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(
        source,
        input_prefix="V1",
        video_prefix=str(tmp_path / "videos"),
        project_root=tmp_path,
    )

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["video_path"] == "__missing_video__"
    assert result.missing_video_ids == ["V1_simple_001"]
    assert result.warnings[0]["问题"] == [
        "missing_video_mapping",
        "video_file_missing_or_empty",
    ]


def test_index_column_takes_priority_over_sequence_when_building_id(
    tmp_path: Path,
):
    video = tmp_path / "direct.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "index": 42,
        "序号": "simple_001",
        "query": "打开设置",
        "video_path": str(video),
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(source, input_prefix="V1", project_root=tmp_path)

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["id"] == "V1_42"
    assert exported["index"] == "42"
    assert exported["序号"] == "simple_001"


def test_index_can_build_id_without_sequence_column(tmp_path: Path):
    video = tmp_path / "direct.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "index": "case_007",
        "query": "关闭设置",
        "video_path": str(video),
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(source, input_prefix="V1", project_root=tmp_path)

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["id"] == "V1_case_007"
    assert exported["index"] == "case_007"
    assert "序号" not in exported


def test_generated_standard_field_keeps_raw_collision_with_numbered_suffix(
    tmp_path: Path,
):
    video = tmp_path / "direct.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "id": "raw-id",
        "id_1": "already-numbered",
        "index": "simple_001",
        "query": "打开设置",
        "video_path": str(video),
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(source, input_prefix="V1", project_root=tmp_path)

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["id"] == "V1_simple_001"
    assert exported["id_1"] == "raw-id"
    assert exported["id_2"] == "already-numbered"


def test_auto_sheet_prefers_merged_data(tmp_path: Path):
    video = tmp_path / "direct.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "cases.xlsx"
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        pd.DataFrame([{"note": "不应读取"}]).to_excel(
            writer,
            sheet_name="设备原始",
            index=False,
        )
        pd.DataFrame([{
            "index": "simple_001",
            "query": "打开设置",
            "video_path": str(video),
        }]).to_excel(writer, sheet_name="合并数据", index=False)

    result = convert_table(
        source,
        input_prefix="V1",
        sheet="auto",
        project_root=tmp_path,
    )

    assert result.selected_sheet == "合并数据"
    assert result.rows[0]["query"] == "打开设置"


def test_windows_gb18030_csv_is_detected_automatically(tmp_path: Path):
    video = tmp_path / "录屏.mp4"
    video.write_bytes(b"video")
    source = tmp_path / "windows_cases.csv"
    pd.DataFrame([{
        "index": "中文_001",
        "query": "打开蓝牙设置",
        "video_path": str(video),
    }]).to_csv(source, index=False, encoding="gb18030")

    result = convert_table(source, input_prefix="Windows", project_root=tmp_path)

    exported = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert exported["id"] == "Windows_中文_001"
    assert exported["query"] == "打开蓝牙设置"
