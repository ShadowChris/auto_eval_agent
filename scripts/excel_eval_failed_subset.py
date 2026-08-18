"""根据评估 Excel 中的执行失败记录，从原始 JSONL 提取补充数据集。

默认读取“逐题结果”，选取 error 非空且 error 不包含
“视频文件不存在”的数据。通过 item_id 对齐原始 JSONL 的 id，
并保持原始数据集顺序输出。

示例：

    python scripts/excel_eval_failed_subset.py \
      evaluation.xlsx \
      dataset.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pandas as pd


DEFAULT_SHEET = "逐题结果"
DEFAULT_EXCLUDED_ERRORS = ("视频文件不存在",)
ITEM_ID_COLUMN = "item_id"
SEQUENCE_COLUMN = "序号"
QUERY_COLUMN = "query"
ERROR_COLUMN = "error"
STATUS_COLUMN = "评估状态"
FAILED_STATUS = "评估失败"


@dataclass(frozen=True)
class FailedSubsetItem:
    item_id: str
    sequence: str
    query: str
    error: str


@dataclass(frozen=True)
class FailedSubsetResult:
    output_path: Path
    evaluation_rows: int
    failed_rows: int
    excluded_rows: int
    selected_rows: int
    source_rows: int
    query_mismatch_ids: list[str]
    items: list[FailedSubsetItem]


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _read_evaluation(path: Path, sheet: str | int) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".xls", ".xlsm"}:
        raise ValueError("评估结果必须是 .xlsx/.xls/.xlsm 文件")
    try:
        return pd.read_excel(
            path,
            sheet_name=sheet,
            dtype=object,
            keep_default_na=False,
        )
    except ValueError as exc:
        raise ValueError(f"无法读取工作表 {sheet}：{exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            content = raw_line.strip()
            if not content:
                continue
            try:
                row = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"原始 JSONL 第 {line_number} 行不是有效 JSON：{exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(f"原始 JSONL 第 {line_number} 行必须是 JSON 对象")
            item_id = _text(row.get("id"))
            if not item_id:
                raise ValueError(f"原始 JSONL 第 {line_number} 行缺少 id")
            if item_id in seen_ids:
                raise ValueError(
                    f"原始 JSONL id 重复：{item_id}（第 {seen_ids[item_id]} 与 "
                    f"{line_number} 行）"
                )
            seen_ids[item_id] = line_number
            rows.append(row)
    return rows


def extract_failed_subset(
    evaluation_path: Path,
    dataset_path: Path,
    *,
    output_path: Path | None = None,
    sheet: str | int = DEFAULT_SHEET,
    excluded_errors: Sequence[str] = DEFAULT_EXCLUDED_ERRORS,
) -> FailedSubsetResult:
    evaluation_path = evaluation_path.expanduser().resolve()
    dataset_path = dataset_path.expanduser().resolve()
    if not evaluation_path.is_file():
        raise FileNotFoundError(f"评估 Excel 不存在：{evaluation_path}")
    if not dataset_path.is_file():
        raise FileNotFoundError(f"原始 JSONL 不存在：{dataset_path}")
    if dataset_path.suffix.lower() != ".jsonl":
        raise ValueError("原始数据集必须是 .jsonl 文件")

    frame = _read_evaluation(evaluation_path, sheet)
    required_columns = {ITEM_ID_COLUMN, ERROR_COLUMN}
    missing_columns = sorted(required_columns - set(frame.columns))
    if missing_columns:
        raise ValueError(
            f"工作表 {sheet} 缺少必需列：{', '.join(missing_columns)}"
        )

    normalized_exclusions = [
        _text(pattern).casefold() for pattern in excluded_errors if _text(pattern)
    ]
    selected: dict[str, FailedSubsetItem] = {}
    failed_rows = 0
    excluded_rows = 0
    for row_number, (_, row) in enumerate(frame.iterrows(), start=2):
        error = _text(row.get(ERROR_COLUMN))
        if not error:
            continue
        status = _text(row.get(STATUS_COLUMN)) if STATUS_COLUMN in frame.columns else ""
        if status and status != FAILED_STATUS:
            continue
        failed_rows += 1
        if any(pattern in error.casefold() for pattern in normalized_exclusions):
            excluded_rows += 1
            continue

        item_id = _text(row.get(ITEM_ID_COLUMN))
        if not item_id:
            raise ValueError(f"工作表 {sheet} 第 {row_number} 行缺少 item_id")
        if item_id in selected:
            raise ValueError(f"工作表 {sheet} 存在重复 item_id：{item_id}")
        selected[item_id] = FailedSubsetItem(
            item_id=item_id,
            sequence=_text(row.get(SEQUENCE_COLUMN)),
            query=_text(row.get(QUERY_COLUMN)),
            error=error,
        )

    source_rows = _read_jsonl(dataset_path)
    source_ids = {_text(row.get("id")) for row in source_rows}
    missing_ids = sorted(set(selected) - source_ids)
    if missing_ids:
        preview = "、".join(missing_ids[:20])
        suffix = "……" if len(missing_ids) > 20 else ""
        raise ValueError(
            f"有 {len(missing_ids)} 个失败 item_id 在原始 JSONL 中不存在："
            f"{preview}{suffix}"
        )

    output_rows: list[dict[str, Any]] = []
    query_mismatch_ids: list[str] = []
    ordered_items: list[FailedSubsetItem] = []
    for source_row in source_rows:
        item_id = _text(source_row.get("id"))
        failed_item = selected.get(item_id)
        if failed_item is None:
            continue
        output_row = dict(source_row)
        if failed_item.sequence:
            output_row.setdefault(SEQUENCE_COLUMN, failed_item.sequence)
        source_query = _text(source_row.get(QUERY_COLUMN))
        if failed_item.query and source_query != failed_item.query:
            query_mismatch_ids.append(item_id)
        output_rows.append(output_row)
        ordered_items.append(failed_item)

    output_path = (
        output_path.expanduser().resolve()
        if output_path is not None
        else dataset_path.with_name(f"{dataset_path.stem}_补充.jsonl")
    )
    if output_path in {dataset_path, evaluation_path}:
        raise ValueError("输出路径不能覆盖输入文件")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for row in output_rows:
            output.write(json.dumps(row, ensure_ascii=False, allow_nan=False))
            output.write("\n")

    return FailedSubsetResult(
        output_path=output_path,
        evaluation_rows=len(frame),
        failed_rows=failed_rows,
        excluded_rows=excluded_rows,
        selected_rows=len(output_rows),
        source_rows=len(source_rows),
        query_mismatch_ids=query_mismatch_ids,
        items=ordered_items,
    )


def _parse_sheet(value: str) -> str | int:
    value = value.strip()
    return int(value) if value.isdigit() else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "根据评估 Excel 中非视频缺失的执行失败记录，"
            "从原始 JSONL 提取补充数据集"
        )
    )
    parser.add_argument("evaluation_excel", type=Path, help="评估导出 Excel")
    parser.add_argument("dataset_jsonl", type=Path, help="原始 JSONL 数据集")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出 JSONL；默认为 <原数据集名>_补充.jsonl",
    )
    parser.add_argument(
        "--sheet",
        type=_parse_sheet,
        default=DEFAULT_SHEET,
        help="逐题结果工作表名称或从 0 开始的序号；默认‘逐题结果’",
    )
    parser.add_argument(
        "--exclude-error",
        action="append",
        dest="excluded_errors",
        help=(
            "排除包含指定文本的 error，可重复使用；"
            "默认排除‘视频文件不存在’"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exclusions = (
        args.excluded_errors
        if args.excluded_errors is not None
        else DEFAULT_EXCLUDED_ERRORS
    )
    try:
        result = extract_failed_subset(
            args.evaluation_excel,
            args.dataset_jsonl,
            output_path=args.output,
            sheet=args.sheet,
            excluded_errors=exclusions,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    print("===== 评估失败补充集提取完成 =====")
    print(f"评估逐题记录：{result.evaluation_rows} 条")
    print(f"评估失败（error 非空）：{result.failed_rows} 条")
    print(f"已排除错误：{result.excluded_rows} 条")
    print(f"补充集：{result.selected_rows} 条")
    for item in result.items:
        label = item.sequence or item.item_id
        print(f"- {label}：{item.query} | {item.error}")
    if result.query_mismatch_ids:
        print(
            "警告：Excel 与原始 JSONL 的 query 不一致："
            + "、".join(result.query_mismatch_ids)
        )
    print(f"JSONL：{result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
