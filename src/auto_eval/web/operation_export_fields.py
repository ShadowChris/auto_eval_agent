"""任务类面向用户的评估结果导出字段。

单数据组与对比导出共同使用这里的字段顺序和展示名称，避免维护两套白名单。
"""
from __future__ import annotations

from typing import Any


OPERATION_RESULT_FIELD_LABELS = {
    "card_presence": "是否挂卡",
    "card_types": "卡片类型",
    "card_suitability": "卡片合适判断",
    "card_suitability_reason": "卡片合适判断原因",
}

OPERATION_USER_RESULT_COLUMNS = (
    "数据集序号",
    "id",
    "metadata.id",
    "item_id",
    "index",
    "session_id",
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
