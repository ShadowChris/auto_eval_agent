"""原生思考的观测信息；不根据模型名称或正文 analysis 推断思考状态。"""
from __future__ import annotations

from typing import Any


def thinking_metadata(
    reasoning_content: str | None,
    usage: Any,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """区分请求设置、实际思考证据及原文是否已记录。"""
    if isinstance(usage, dict):
        tokens = usage.get("reasoning_tokens")
    else:
        tokens = getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None,
        )
    mode = "unknown"
    if request is not None:
        extra = request.get("extra_body") or {}
        enabled = extra.get("enable_thinking", request.get("enable_thinking"))
        mode = (
            "enabled" if enabled is True
            else "disabled" if enabled is False
            else "provider_default"
        )
    return {
        "request_mode": mode,
        "status": "confirmed" if reasoning_content or (tokens or 0) > 0 else "unconfirmed",
        "reasoning_tokens": tokens,
        "content_status": (
            "not_recorded" if reasoning_content is None
            else "recorded" if reasoning_content
            else "not_returned"
        ),
    }


def thinking_summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """按轮汇总，缺少原文或用量时保持未知，不误标为关闭。"""
    metadata = [
        row.get("thinking") or thinking_metadata(
            row.get("reasoning_content"), row.get("usage"),
        )
        for row in rounds
    ]
    tokens = [row["reasoning_tokens"] for row in metadata if row["reasoning_tokens"] is not None]
    confirmed = sum(row["status"] == "confirmed" for row in metadata)
    return {
        "status": "confirmed" if confirmed else "unconfirmed",
        "total_rounds": len(metadata),
        "confirmed_rounds": confirmed,
        "reasoning_tokens": sum(tokens) if tokens else None,
        "usage_reported_rounds": len(tokens),
        "content_recorded_rounds": sum(row["content_status"] == "recorded" for row in metadata),
        "content_not_recorded_rounds": sum(row["content_status"] == "not_recorded" for row in metadata),
    }


def annotate_reasoning_trace(record: dict[str, Any]) -> dict[str, Any]:
    """为新旧裁判调用生成统一说明，保留旧记录缺失原文的事实。"""
    rounds = []
    for row in record.get("llm_rounds") or []:
        rounds.append({
            **row,
            "thinking": row.get("thinking") or thinking_metadata(
                row.get("reasoning_content"), row.get("usage"),
            ),
        })
    return {**record, "llm_rounds": rounds, "thinking_summary": thinking_summary(rounds)}
