# 项目辅助脚本

## Excel/CSV 转任务类 JSONL

`data/excel_to_jsonl_dataset.py` 用于将 `.xlsx`、`.xls`、`.xlsm` 或 `.csv`
表格按原顺序转换为 Web 评测台可批量导入的任务类 JSONL。

首次使用时安装数据处理依赖：

```bash
python -m pip install -e ".[data]"
```

当前 V1 数据的调用方式：

```bash
python data/excel_to_jsonl_dataset.py \
  "data/0805/V1/V1_录屏0805_复杂任务.csv" \
  --input-prefix "V1_录屏0805" \
  --video-prefix "data/0805/V1"
```

未指定 `--output` 时，输出到输入文件同目录的同名 `.jsonl`：

```text
data/0805/V1/V1_录屏0805_复杂任务.jsonl
```

命令行会输出转换行数、空白行数、警告行数、缺失视频数量以及最终
JSONL 路径。有警告的数据仍会保序输出，便于后续在评测结果中对齐定位。

常用参数：

- `--input-prefix`：生成题号的前缀，题号格式为 `<input-prefix>_<序号>`。
- `--video-prefix`：拼接录屏路径的项目相对前缀。
- `--output`：显式指定输出 JSONL 路径。
- `--current-location`：源表没有位置时写入 `context` 的默认当前位置。
- `--sheet`：Excel 工作表名称或从 `0` 开始的序号。
- `--encoding`：CSV 编码，默认为 `utf-8-sig`。

录屏路径拼接规则：

- `文件路径` 与 `原文件名` 都有值：`video-prefix/文件路径/原文件名`。
- `文件路径` 为空、`原文件名` 有值：`video-prefix/原文件名`。
- `原文件名` 为空但 `video_path` 有值：直接使用 `video_path`。
- `原文件名` 和 `video_path` 均为空：输出缺失占位路径并记录警告。

只有列名精确为 `文件路径` 时才会参与拼接；备注列（例如 `文件路径1`）
不会被当作录屏路径。

查看完整参数：

```bash
python data/excel_to_jsonl_dataset.py --help
```

## FFmpeg 抽帧环境验证

```bash
python scripts/check_ffmpeg.py
```

该脚本会生成一段临时视频，并验证项目实际的时长探测和关键帧抽取链路。
