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
from ..judges.operation_fields import LEGACY_REVIEW_CATEGORIES, split_operation_issue_fields
from .operation_export_fields import (
    operation_result_identity_fields,
    operation_user_result_display_row,
)


SUPPORTED_SUFFIXES = {".jsonl", ".csv", ".xlsx", ".xls", ".xlsm"}
PREFERRED_RESULT_SHEETS = ("评估结果", "逐题结果", "逐题-任务类")
REQUIRED_COMPARISON_FIELDS = {
    "id": ("id", "item_id", "题号"),
    "query": ("query", "操作意图", "问题"),
    "correctness": ("correctness", "完成判定", "判定"),
    "issue_types": ("issue_types", "issue_type", "问题类型"),
    "rationale": ("rationale", "判定理由"),
}
COMPARISON_FIELD_ALIASES = {
    **REQUIRED_COMPARISON_FIELDS,
    "need_review_cate": ("need_review_cate", "需复核类别"),
}


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
    mapping = {"index": "", **dict.fromkeys(COMPARISON_FIELD_ALIASES, "")}
    for position, record in enumerate(records):
        row, row_mapping, row_warnings = _normalize_record(record, position)
        rows.append(row)
        for key, value in row_mapping.items():
            if value and not mapping[key]:
                mapping[key] = value
        for warning in row_warnings:
            warnings.append({"row": position + 2, "message": warning})
    missing = [field for field in REQUIRED_COMPARISON_FIELDS if not mapping[field]]
    if missing:
        raise ValueError(f"缺少必需列：{'、'.join(missing)}；请上传包含所有必需字段的评估结果表")
    warnings.extend(_exclude_duplicate_ids(rows))
    valid_count = _valid_row_count(rows)
    if not valid_count:
        details = "；".join(warning["message"] for warning in warnings[:3])
        raise ValueError(f"没有可参与对比的有效记录：{details or '所有记录均为评估失败'}")

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
        exported = row.get("export") if isinstance(row.get("export"), dict) else {}
        normalized, _, _ = _normalize_record({
            **exported,
            "id": row.get("id") or row.get("item_id") or "",
            "index": row.get("index", ""),
            "case_id": row.get("case_id", ""),
            "query": row.get("query", ""),
            "evaluation": dict(result),
        }, position)
        normalized_rows.append(normalized)
    _exclude_duplicate_ids(normalized_rows)
    if not _valid_row_count(normalized_rows):
        raise ValueError("上传结果集没有可参与对比的有效记录")
    return {
        "task_id": source_id,
        "source_type": "upload",
        "dataset_name": str(
            source.get("group_name") or source.get("dataset_name") or source_id
        ),
        "created_at": None,
        "judge_provider": "",
        "judge_model": "",
        "rows": normalized_rows,
    }


def _valid_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        not row["result"].get("error")
        and row["result"].get("correctness") in OPERATION_CORRECTNESS
        for row in rows
    )


def _exclude_duplicate_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions: dict[str, list[int]] = {}
    for position, row in enumerate(rows):
        if row["id"]:
            positions.setdefault(row["id"], []).append(position)
    warnings = []
    for item_id, indexes in positions.items():
        if len(indexes) < 2:
            continue
        message = f"id={item_id} 重复，重复数据不会参与自动对齐"
        warnings.append({"row": indexes[0] + 2, "message": message})
        for index in indexes:
            for key in ("result", "export"):
                rows[index][key]["error"] = rows[index][key].get("error") or message
    return warnings


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
                dataframe = pd.read_csv(BytesIO(content), encoding=encoding, dtype=object)
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
    dataframe = pd.read_excel(workbook, sheet_name=sheet, dtype=object)
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
    field_values = {}
    field_mapping = {}
    for field, aliases in COMPARISON_FIELD_ALIASES.items():
        value, key = (
            _field(record, aliases)
            if field in {"id", "query"}
            else _result_field(evaluation, record, aliases)
        )
        field_values[field] = value
        field_mapping[field] = key
    match_index, index_key = _field(record, ("index",))
    case_id, _ = _field(record, ("case_id", "caseId", "case id"))
    query = field_values["query"]
    item_id = field_values["id"]
    correctness = field_values["correctness"]
    issue_types = field_values["issue_types"]
    review_categories = field_values["need_review_cate"]
    error, _ = _result_field(evaluation, record, ("error", "错误"))
    rationale = str(field_values["rationale"] or "").strip()
    correctness = str(correctness or "").strip().lower()
    raw_correctness = correctness
    if correctness == "others":
        correctness = "no_support"
    problems = []
    missing = [field for field in REQUIRED_COMPARISON_FIELDS if not field_mapping[field]]
    if missing:
        problems.append(f"缺少必需字段：{'、'.join(missing)}")
    for field, value in (("id", item_id), ("query", query)):
        if value is None or not str(value).strip():
            problems.append(f"{field} 不能为空")
    # 系统原生失败记录没有评分和判定理由，但需保留其行用于追溯。
    if not error:
        if correctness not in OPERATION_CORRECTNESS:
            problems.append("correctness 必须是 ok、nok 或 no_support")
        if not rationale:
            problems.append("rationale 不能为空")
    warnings = ["；".join(problems)] if problems else []
    if problems:
        error = f"导入校验失败：{'；'.join(problems)}"

    result = dict(evaluation)
    effect_issues, legacy_review = split_operation_issue_fields(
        normalize_operation_issue_types(issue_types), LEGACY_REVIEW_CATEGORIES,
    )
    result.update({
        "correctness": correctness,
        "issue_types": effect_issues,
        "need_review_cate": list(dict.fromkeys([
            *normalize_operation_issue_types(review_categories), *legacy_review,
        ])),
        "rationale": rationale,
        "error": str(error or "").strip(),
    })
    if raw_correctness == "others":
        result["judge_correctness"] = raw_correctness
    export = _standard_export(record, evaluation, result)
    resolved_item_id = str(item_id if item_id is not None else "").strip()
    export["id"] = resolved_item_id
    export["index"] = match_index
    export["item_id"] = resolved_item_id
    export["case_id"] = str(case_id or "")
    export["query"] = str(query or "")
    return ({
        "position": position,
        "id": resolved_item_id,
        "index": match_index,
        "item_id": resolved_item_id,
        "case_id": str(case_id or "").strip(),
        "query": str(query or "").strip(),
        "result": result,
        "export": export,
    }, {
        "index": index_key,
        **field_mapping,
    }, warnings)


def _standard_export(
    record: dict[str, Any],
    evaluation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    aliases = {
        "数据集序号": ("数据集序号",),
        "id": ("id", "item_id", "题号"),
        "item_id": ("item_id", "id", "题号"),
        "index": ("index",),
        "序号": ("序号", "sequence"),
        "session_id": ("session_id", "sessionid", "sessionId"),
        "sessionid": ("sessionid", "session_id", "sessionId"),
        "query": ("query", "操作意图", "问题"),
        "context": ("context",),
        "attachment_path": ("attachment_path",),
        "attachment_url_domain": ("attachment_url_domain",),
        "attachment_url_ip": ("attachment_url_ip",),
        "answer": ("answer", "agent_statement", "回复内容"),
        "need_review_cate": ("need_review_cate", "需复核类别"),
        "card_presence": ("card_presence", "是否挂卡"),
        "card_types": ("card_types", "卡片类型"),
        "card_suitability": ("card_suitability", "卡片合适判断"),
        "card_suitability_reason": (
            "card_suitability_reason",
            "卡片合适判断原因",
        ),
        "rationale": ("rationale",),
        "is_low_level": ("is_low_level",),
        "total": ("total",),
        "维度_操作完成度": ("维度_操作完成度",),
        "理由_操作完成度": ("理由_操作完成度",),
        "维度_步骤正确性": ("维度_步骤正确性",),
        "理由_步骤正确性": ("理由_步骤正确性",),
        "latency_s": ("latency_s",),
        "分享链接": ("分享链接", "share_url", "share_link"),
        "video_path": ("video_path", "录屏项目相对路径"),
        "录屏URL": ("录屏URL", "录屏url", "video_url", "视频链接"),
        "video_url_domain": ("video_url_domain",),
        "video_url_ip": ("video_url_ip",),
        "录屏时长": ("录屏时长", "耗时", "录屏时长（秒）", "duration"),
    }
    export = {}
    for output_key, candidates in aliases.items():
        value, _ = _field(record, candidates)
        export[output_key] = value
    for output_key in (
        "answer",
        "card_presence",
        "card_types",
        "card_suitability",
        "card_suitability_reason",
        "rationale",
        "is_low_level",
        "total",
        "latency_s",
    ):
        value, _ = _result_field(evaluation, record, (output_key,))
        if value not in (None, ""):
            export[output_key] = value
    for output_key in ("execution_routes", "链路类型", "task_type", "duration_s", "rubric", "rubric_reasons"):
        value, _ = _result_field(evaluation, record, (output_key,))
        export[output_key] = value
    rubric = export.get("rubric") if isinstance(export.get("rubric"), dict) else {}
    rubric_reasons = (
        export.get("rubric_reasons")
        if isinstance(export.get("rubric_reasons"), dict)
        else {}
    )
    export["维度_操作完成度"] = export["维度_操作完成度"] or rubric.get("操作完成度", "")
    export["理由_操作完成度"] = export["理由_操作完成度"] or rubric_reasons.get("操作完成度", "")
    export["维度_步骤正确性"] = export["维度_步骤正确性"] or rubric.get("步骤正确性", "")
    export["理由_步骤正确性"] = export["理由_步骤正确性"] or rubric_reasons.get("步骤正确性", "")
    export.update({
        "correctness": result.get("correctness") or "",
        "issue_types": result.get("issue_types") or [],
        "need_review_cate": result.get("need_review_cate") or [],
        "rationale": result.get("rationale") or "",
        "error": result.get("error") or "",
    })
    identities = operation_result_identity_fields(record, export.get("id", ""))
    # Excel/CSV 已有展开列时直接读取，JSONL 则从原始 metadata 读取。
    if "metadata.id" in record:
        identities["metadata.id"] = record["metadata.id"]
    export.update(identities)
    # 上传结果与历史批次共用单数据组“评估结果”的最终展示字段；额外保留
    # 报告使用的兼容字段，不让 Excel 导出与 HTML 报告互相牵制。
    export.update(operation_user_result_display_row(export))
    return export
