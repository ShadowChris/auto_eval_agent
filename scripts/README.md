# 数据处理与项目辅助脚本

## Excel/CSV 转任务类 JSONL

`scripts/excel_to_jsonl_dataset.py` 用于将 `.xlsx`、`.xls`、`.xlsm` 或 `.csv`
表格按原顺序转换为 Web 评测台可批量导入的任务类 JSONL。

首次使用时安装数据处理依赖：

```bash
python -m pip install -e ".[data]"
```

当前 V1 数据的调用方式：

```bash
python scripts/excel_to_jsonl_dataset.py \
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
python scripts/excel_to_jsonl_dataset.py --help
```

## 从评估 Excel 提取失败补充集

`scripts/excel_eval_failed_subset.py` 读取评估导出 Excel 的“逐题结果”，
选出 `error` 非空且不是“视频文件不存在”的评估失败数据，
再按 `item_id` 从原始 JSONL 中提取完整数据。输出保持原数据集顺序，
可直接在 Web 端重新评估。

```bash
python scripts/excel_eval_failed_subset.py \
  "/path/to/数据集_eval_xxx.xlsx" \
  "data/path/原始数据集.jsonl"
```

未指定 `--output` 时，默认输出到原始 JSONL 同目录：

```text
<原数据集名>_补充.jsonl
```

可用 `--sheet` 指定工作表；可重复使用 `--exclude-error` 自定义
需要排除的错误文本。脚本会校验所有失败 `item_id` 均存在于原始
JSONL，避免因数据集版本不一致而静默丢数据。

## 按设备分组合并 CSV/XLSX

`scripts/csv_merge_by_device_group.py` 根据 CSV 或 XLSX 文件名末尾的设备编号，
把多个设备的数据合并成不同组的 CSV。CSV 和 XLSX 可以混合输入；XLSX
固定读取第一个工作表。每行会追加 `device_id` 列，合并结果按“序号”
自然升序排列，输出使用 `utf-8-sig`，可直接用 Excel 打开。

```bash
python scripts/csv_merge_by_device_group.py \
  "data/0817/0817-CSV" \
  --group "实验组:DEVICE_EXP_001;DEVICE_EXP_002;DEVICE_EXP_003" \
  --group "对照组:DEVICE_CTRL_001;DEVICE_CTRL_002;DEVICE_CTRL_003" \
  --output-name "0817设置众测"
```

实际输出文件名分别为：

```text
0817设置众测_实验组.csv
0817设置众测_对照组.csv
```

也可以把示例中的两行分组定义保存成 UTF-8 文本文件，并使用
`--groups-file groups.txt`。默认输出目录为输入目录下的 `merged_output`
文件夹，可用 `--output-dir` 指定。

## FFmpeg 抽帧环境验证

```bash
python scripts/check_ffmpeg.py
```

该脚本会生成一段临时视频，并验证项目实际的时长探测和关键帧抽取链路。
