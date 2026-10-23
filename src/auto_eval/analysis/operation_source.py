"""任务类对比使用的原始数据与共享输入读取。"""
from __future__ import annotations

import json
from typing import Any


def comparison_source_data(row: dict[str, Any]) -> dict[str, Any]:
    """优先使用完整原始记录，兼容尚未携带原始记录的旧对比输入。"""
    source = row.get("source_data")
    if isinstance(source, dict):
        return source
    exported = row.get("export")
    if isinstance(exported, dict):
        return exported
    return {key: row[key] for key in ("id", "item_id", "index", "query") if key in row}


def _shared_inputs(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("input_data")
    source = dict(source) if isinstance(source, dict) else dict(comparison_source_data(row))
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            source.setdefault(str(key), value)
    inputs = {}
    if "context" in source:
        inputs["context"] = str(source["context"] or "").strip()
    image_fields = ("query_images", "query_image_path", "attachment_path")
    if any(field in source for field in image_fields):
        images = []
        for field in image_fields:
            value = source.get(field)
            if isinstance(value, str):
                try:
                    value = json.loads(value) if value.strip().startswith("[") else value
                except json.JSONDecodeError:
                    pass
            candidates = value if isinstance(value, list) else [value]
            images = [str(path).strip() for path in candidates if path and str(path).strip()]
            if images:
                break
        inputs["query_images"] = images
    return inputs


def shared_input_differences(
    baseline: dict[str, Any], target: dict[str, Any],
) -> list[str]:
    """只比较双方都明确提供的共享输入；缺少可选字段不推断为差异。"""
    left, right = _shared_inputs(baseline), _shared_inputs(target)
    return [field for field in left if field in right and left[field] != right[field]]
