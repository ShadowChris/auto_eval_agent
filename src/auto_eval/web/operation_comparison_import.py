"""任务类对比分析的已评估结果文件导入。"""
from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from ..analysis.operation_statistics import (
    OPERATION_CORRECTNESS,
    normalize_operation_issue_types,
)


SUPPORTED_SUFFIXES = {".jsonl", ".csv", ".xlsx", ".xls", ".xlsm"}
PREFERRED_RESULT_SHEETS = ("逐题结果", "逐题-任务类")


def import_operation_comparison_file(
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """读取已评估 JSONL/CSV/Excel，并转换为批次比较的标准行。"""
    safe_name = Path(filename or "comparison_results").name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("仅支持 JSONL、CSV、XLSX、XLS、XLSM 评估结果文件")
    if not content:
        raise ValueError("评估结果文件为空")

    records, sheet = _read_records(content, suffix)
    if not records:
        raise ValueError("文件中没有可导入的数据行")
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    mapping = {
        "index": "",
        "query": "",
        "correctness": "",
        "issue_types": "",
    }
    valid_count = 0
    for position, record in enumerate(records):
        row, row_mapping, row_warnings = _normalize_record(record, position)
        rows.append(row)
        for key, value in row_mapping.items():
            if value and not mapping[key]:
                mapping[key] = value
        for warning in row_warnings:
            warnings.append({"row": position + 2, "message": warning})
        result = row.get("result") or {}
        if not result.get("error") and result.get("correctness") in OPERATION_CORRECTNESS:
            valid_count += 1
    if not mapping["correctness"] or not valid_count:
        raise ValueError(
            "未识别到有效 correctness；请上传系统逐题 JSONL/Excel，"
            "或包含 ok/nok/no_support/others 判定的结果表"
        )
    if not mapping["index"] and not mapping["query"]:
        raise ValueError("未识别到 index 或 query，无法自动对齐数据")

    source_id = f"upload_{uuid.uuid4().hex[:12]}"
    return {
        "source_id": source_id,
        "task_id": source_id,
        "source_type": "upload",
        "dataset_name": safe_name,
        "group_name": safe_name,
        "rows": rows,
        "mapping": mapping,
        "warnings": warnings,
        "summary": {
            "format": suffix.lstrip(".").upper(),
            "sheet": sheet,
            "raw_count": len(records),
            "valid_count": valid_count,
            "invalid_count": len(records) - valid_count,
            "warning_count": len(warnings),
        },
    }


def validate_uploaded_comparison_source(source: dict[str, Any]) -> dict[str, Any]:
    """限制并清洗客户端回传的上传数据源。"""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id.startswith("upload_"):
        raise ValueError("上传结果集 source_id 无效")
    rows = source.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("上传结果集没有数据行")
    if len(rows) > 100_000:
        raise ValueError("单个上传结果集不能超过 100000 行")
    normalized_rows = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"上传结果集第 {position + 1} 行格式无效")
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        normalized_rows.append({
            "position": position,
            "index": row.get("index") if row.get("index") is not None else "",
            "item_id": str(row.get("item_id") or f"q{position}"),
            "case_id": str(row.get("case_id") or ""),
            "query": str(row.get("query") or ""),
            "result": dict(result),
            "export": dict(row.get("export") or {}),
        })
    return {
        "task_id": source_id,
        "dataset_name": str(
            source.get("group_name") or source.get("dataset_name") or source_id
        ),
        "created_at": None,
        "judge_provider": "",
        "judge_model": "",
        "rows": normalized_rows,
    }


def _read_records(
    content: bytes,
    suffix: str,
) -> tuple[list[dict[str, Any]], str | int | None]:
    if suffix == ".jsonl":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("JSONL 必须使用 UTF-8 编码") from exc
        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_number} 行不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL 第 {line_number} 行必须是对象")
            records.append(value)
        return records, None
    if suffix == ".csv":
        dataframe = None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                dataframe = pd.read_csv(BytesIO(content), encoding=encoding)
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        if dataframe is None:
            raise ValueError("CSV 编码无法识别，请使用 UTF-8 或 GB18030") from last_error
        return _dataframe_records(dataframe), None

    workbook = pd.ExcelFile(BytesIO(content))
    sheet = next(
        (name for name in PREFERRED_RESULT_SHEETS if name in workbook.sheet_names),
        workbook.sheet_names[0] if workbook.sheet_names else None,
    )
    if sheet is None:
        return [], None
    dataframe = pd.read_excel(workbook, sheet_name=sheet)
    return _dataframe_records(dataframe), sheet


def _dataframe_records(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    dataframe = dataframe.dropna(how="all")
    return [
        {str(key).strip(): _json_value(value) for key, value in record.items()}
        for record in dataframe.to_dict(orient="records")
    ]


def _json_value(value: Any) -> Any:
    if value is None:
        return ""
    if not isinstance(value, (str, bytes)):
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _field(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str]:
    normalized = {str(key).strip().casefold(): key for key in record}
    for alias in aliases:
        key = normalized.get(alias.casefold())
        if key is not None:
            return record.get(key), str(key)
    return "", ""


def _result_field(
    evaluation: dict[str, Any],
    record: dict[str, Any],
    aliases: tuple[str, ...],
) -> tuple[Any, str]:
    value, key = _field(evaluation, aliases)
    if key:
        return value, f"evaluation.{key}"
    return _field(record, aliases)


def _normalize_record(
    record: dict[str, Any],
    position: int,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        evaluation = {}
    match_index, index_key = _field(record, ("index",))
    case_id, _ = _field(record, ("case_id", "caseId", "case id"))
    query, query_key = _field(record, ("query", "操作意图", "问题"))
    item_id, _ = _field(record, ("item_id", "id", "题号"))
    correctness, correctness_key = _result_field(
        evaluation,
        record,
        ("correctness", "完成判定", "判定"),
    )
    issue_types, issue_key = _result_field(
        evaluation,
        record,
        ("issue_types", "issue_type", "问题类型"),
    )
    error, _ = _result_field(evaluation, record, ("error", "错误"))
    correctness = str(correctness or "").strip().lower()
    warnings = []
    if correctness and correctness not in OPERATION_CORRECTNESS:
        warnings.append(f"correctness={correctness!r} 不在允许值中，按无效结果处理")
        correctness = ""
    if (
        not str(match_index if match_index is not None else "").strip()
        and not str(query or "").strip()
    ):
        warnings.append("缺少 index 和 query，无法参与自动匹配")

    result = dict(evaluation)
    result.update({
        "correctness": correctness,
        "issue_types": normalize_operation_issue_types(issue_types),
        "error": str(error or "").strip(),
    })
    export = _standard_export(record, evaluation, result)
    resolved_item_id = str(item_id or f"q{position}")
    export["index"] = match_index
    export["item_id"] = resolved_item_id
    export["case_id"] = str(case_id or "")
    export["query"] = str(query or "")
    return ({
        "position": position,
        "index": match_index,
        "item_id": resolved_item_id,
        "case_id": str(case_id or "").strip(),
        "query": str(query or "").strip(),
        "result": result,
        "export": export,
    }, {
        "index": index_key,
        "query": query_key,
        "correctness": correctness_key,
        "issue_types": issue_key,
    }, warnings)


def _standard_export(
    record: dict[str, Any],
    evaluation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    aliases = {
        "index": ("index",),
        "序号": ("序号", "sequence"),
        "sessionid": ("sessionid", "session_id", "sessionId"),
        "answer": ("answer", "agent_statement", "回复内容"),
        "context": ("context",),
        "分享链接": ("分享链接", "share_url", "share_link"),
        "video_path": ("video_path", "录屏项目相对路径"),
        "录屏URL": ("录屏URL", "录屏url", "video_url", "视频链接"),
        "video_url_domain": ("video_url_domain",),
        "video_url_ip": ("video_url_ip",),
    }
    export = {}
    for output_key, candidates in aliases.items():
        value, _ = _field(record, candidates)
        export[output_key] = value
    for output_key in (
        "is_low_level",
        "execution_routes",
        "链路类型",
        "rationale",
        "task_type",
        "duration_s",
        "latency_s",
        "rubric",
        "rubric_reasons",
    ):
        value, _ = _result_field(evaluation, record, (output_key,))
        export[output_key] = value
    export.update({
        "correctness": result.get("correctness") or "",
        "issue_types": result.get("issue_types") or [],
        "error": result.get("error") or "",
    })
    return export
