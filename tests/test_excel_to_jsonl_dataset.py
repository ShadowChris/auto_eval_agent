import json
from pathlib import Path

import pytest


pd = pytest.importorskip("pandas")

from scripts.excel_to_jsonl_dataset import convert_table


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
        "当前时间：2026-08-05 10:00:00；当前位置：浙江省杭州市滨江区"
    )
    assert exported[0]["video_path"].endswith("videos/simple_001/record.mp4")
    assert exported[0]["分享链接"] == "https://example.test/1"
    assert "answer" not in exported[1]
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
