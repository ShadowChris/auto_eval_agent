"""任务类面向用户的评估结果导出字段。

单数据组与对比导出共同使用这里的字段顺序和展示名称，避免维护两套白名单。
"""
from __future__ import annotations

from typing import Any


OPERATION_CARD_FIELD_LABELS = {
    "card_presence": "是否挂卡",
    "card_types": "卡片类型",
    "card_suitability": "卡片合适判断",
    "card_suitability_reason": "卡片合适判断原因",
}

OPERATION_RESULT_FIELD_LABELS = {
    **OPERATION_CARD_FIELD_LABELS,
    "conversation_id": "评估会话分组",
    "turn_index": "组内轮次",
}

# Execution/history metadata belongs in 数据集明细 and JSONL, not result sheets.
# Multi-group case_id has a different purpose and must not be filtered there.
OPERATION_INTERNAL_ROUND_FIELDS = ("case_id", "turn_count", "is_result_row", "evaluation_scope")

OPERATION_USER_RESULT_COLUMNS = (
    "数据集序号",
    "id",
    "metadata.id",
    "item_id",
    "index",
    "session_id",
    "conversation_id",
    "turn_index",
    "is_start",
    "is_end",
    "query",
    "context",
    "attachment_path",
    "attachment_url_domain",
    "attachment_url_ip",
    "answer",
    "correctness",
    "issue_types",
    "need_review_cate",
    "card_presence",
    "card_types",
    "card_suitability",
    "card_suitability_reason",
    "rationale",
    "is_low_level",
    "total",
    "维度_操作完成度",
    "理由_操作完成度",
    "维度_步骤正确性",
    "理由_步骤正确性",
    "latency_s",
    "分享链接",
    "video_path",
    "video_url_domain",
    "video_url_ip",
    "录屏时长",
    "task_start_time",
    "task_end_time",
)

OPERATION_USER_RESULT_HEADERS = tuple(
    OPERATION_RESULT_FIELD_LABELS.get(field, field)
    for field in OPERATION_USER_RESULT_COLUMNS
)


def operation_result_identity_fields(
    source: dict[str, Any], item_id: Any = "",
) -> dict[str, Any]:
    """分别展示输入 id 与 metadata.id，保留两者的原始值。"""
    metadata = source.get("metadata")
    metadata_id = metadata.get("id", "") if isinstance(metadata, dict) else ""
    return {
        "id": source.get("id", item_id),
        "metadata.id": metadata_id,
    }


def operation_user_result_display_row(row: dict[str, Any]) -> dict[str, Any]:
    """按统一顺序生成带用户可见表头的任务类评估结果行。"""
    return {
        OPERATION_RESULT_FIELD_LABELS.get(field, field): row.get(field, "")
        for field in OPERATION_USER_RESULT_COLUMNS
    }


def operation_round_export_fields(
    item: dict[str, Any], source: dict[str, Any],
) -> dict[str, Any]:
    """导出已记录的会话信息，不根据剩余行数猜测开始或结束边界。"""
    def first(*values: Any) -> Any:
        return next((value for value in values if value is not None and value != ""), "")

    # A latest-turn item contains history and current input, never future turns.
    turns = item.get("turns") or []
    current = turns[-1] if item.get("evaluation_scope") == "latest_turn" and turns else {}
    current = current if isinstance(current, dict) else {}
    current_source = current.get("source_data") or {}
    current_source = current_source if isinstance(current_source, dict) else {}
    current_metadata = current_source.get("metadata")
    current_metadata = current_metadata if isinstance(current_metadata, dict) else {}
    fields = {
        "conversation_id": first(item.get("conversation_id"), source.get("conversation_id")),
        "turn_index": first(item.get("turn_index"), source.get("turn_index")),
    }
    for key in ("is_start", "is_end"):
        fields[key] = first(source.get(key), item.get(key), current_source.get(key),
                            current_metadata.get(key), current.get(key))
    return fields
