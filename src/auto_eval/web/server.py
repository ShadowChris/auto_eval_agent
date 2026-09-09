"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8501）
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware

from ..analysis.operation_comparison import compare_operation_batches
from ..analysis.operation_report import build_comparison_report, build_single_report
from ..config import ExpertKnowledgeBase, load_config
from ..expert_knowledge import ExpertKnowledgeStore, render_expert_knowledge
from ..media import extract_scene_keyframes, probe_duration
from ..paths import RUNS_DIR
from ..report.operation import OPERATION_REPORT_ASSETS, build_operation_report_html
from ..table_dataset import convert_table
from ..llm_stream import build_openai_client, stream_chat_completion
from .parse_input import Mode, parse_jsonl, parse_text
from .history import (
    build_operation_comparison_xlsx,
    build_xlsx,
    delete_snapshot,
    dataset_artifact_path,
    export_rows,
    jsonl_export_rows,
    list_snapshots,
    list_snapshots_page,
    load_item_judge_calls,
    load_dataset_artifact,
    load_snapshot,
    operation_item_result_row,
    operation_comparison_batch,
    operation_statistics_payload,
    rows_to_csv,
    rows_to_jsonl,
    save_task,
    save_dataset_batch_snapshot,
    save_dataset_rollback_snapshot,
    snapshot_payload,
    task_to_snapshot,
    write_frames_zip,
)
from .dataset_revision import (
    ACTIVE as DATASET_ACTIVE,
    EXCLUDED as DATASET_EXCLUDED,
    active_result_count,
    active_total,
    is_item_active,
    tracked_item,
)
from .operation_media import (
    MAX_QUERY_IMAGE_BYTES,
    QUERY_IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    operation_video_roots,
    prepare_cached_operation_item,
    resolve_operation_query_image_path,
    resolve_operation_video_path,
)
from .operation_groups import align_operation_groups
from .operation_comparison_import import (
    SUPPORTED_SUFFIXES as COMPARISON_IMPORT_SUFFIXES,
    import_operation_comparison_file,
    validate_uploaded_comparison_source,
)
from .llm_providers import LLMProviderPayload, LLMProviderStore
from .runner import (
    refresh_task_summary,
    run_append,
    run_eval,
    run_rerun,
    run_single_api_item,
)
from .tasks import (
    get_live_task,
    get_task,
    new_task,
    prune_task_cache,
    remove_task,
)

# auto_eval_agent/ 目录（src/auto_eval/web/server.py 往上 4 层）
BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_VERSION_TOKEN = "__STATIC_ASSET_VERSION__"


def _static_asset_version() -> str:
    """按前端资源内容生成短版本号，发布新文件后自动改变 URL。"""
    digest = hashlib.sha256()
    for name in ("app.js", "style.css"):
        digest.update((STATIC_DIR / name).read_bytes())
    for name in ("operation_report.js", "operation_report.css"):
        digest.update((OPERATION_REPORT_ASSETS / name).read_bytes())
    return digest.hexdigest()[:12]


class VersionedStaticFiles(StaticFiles):
    """版本化资源长期缓存；无版本参数的资源每次重新验证。"""

    async def get_response(self, path: str, scope: dict) -> Response:
        response = await super().get_response(path, scope)
        query = scope.get("query_string") or b""
        if response.status_code == 200 and b"v=" in query:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

load_dotenv(BASE_DIR / ".env", override=True)  # 注入 .env 的 key；以 .env 为准覆盖旧 shell 环境变量

app = FastAPI(title="auto_eval 评估台")
app.add_middleware(GZipMiddleware, minimum_size=1024)
_state: dict = {}


@app.on_event("startup")
def _load():
    _state["cfg"] = load_config(CONFIG_DIR)


def cfg():
    return _state["cfg"]


def _operation_knowledge_store() -> ExpertKnowledgeStore:
    return ExpertKnowledgeStore(
        CONFIG_DIR / "knowledge" / "operation.yaml",
        RUNS_DIR / "knowledge_drafts" / "operation.yaml",
    )


class ParseReq(BaseModel):
    mode: Mode
    text: str | None = None
    jsonl: str | None = None


class EvalReq(BaseModel):
    mode: Mode
    items: list[dict]
    options: dict = Field(default_factory=dict)
    dataset_name: str = ""
    submit_mode: Literal["create", "append"] = "create"
    task_id: str = ""
    conflict_policy: Literal["reject", "preview", "smart", "resolve"] = "reject"
    conflict_resolutions: dict[
        str,
        Literal["keep_existing", "replace_and_rerun"],
    ] = Field(default_factory=dict)


class SingleEvalReq(BaseModel):
    task_id: str
    dataset_name: str = ""
    item: dict


class OperationPrepareReq(BaseModel):
    items: list[dict]
    concurrency: int = 2


class OperationGroupManifest(BaseModel):
    group_id: str
    group_name: str
    group_role: str = "experiment"
    dataset_name: str = ""
    jsonl: str


class OperationGroupsAlignReq(BaseModel):
    groups: list[OperationGroupManifest]


class HistoryNoteReq(BaseModel):
    note: str = ""


class OperationHistoryComparisonReq(BaseModel):
    task_ids: list[str]
    baseline_task_id: str


class OperationComparisonSourceReq(BaseModel):
    source_id: str
    source_type: Literal["history", "upload"]
    task_id: str = ""
    dataset_name: str = ""
    group_name: str = ""
    rows: list[dict] = Field(default_factory=list)


class OperationComparisonAnalyzeReq(BaseModel):
    sources: list[OperationComparisonSourceReq]
    control_source_id: str


class RerunReq(BaseModel):
    item_indices: list[int]
    judge_backend: dict | None = None


class DatasetItemsActionReq(BaseModel):
    item_indices: list[int]
    reason: str = ""


class DatasetBatchRollbackReq(BaseModel):
    reason: str = ""


class ProviderTestReq(BaseModel):
    model: str = ""


_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS


def _operation_video_roots() -> list[Path]:
    return operation_video_roots(BASE_DIR)


def _llm_provider_store() -> LLMProviderStore:
    return LLMProviderStore(RUNS_DIR / "web_settings")


def _runtime_config_for_options(app_cfg, options: dict):
    """按任务快照构造隔离配置；绝不修改进程共享的 AppConfig。"""
    backend = options.get("judge_backend") or {}
    provider_id = str(backend.get("provider_id") or "").strip()
    if not provider_id:
        return app_cfg
    resolution = _llm_provider_store().resolve(
        provider_id,
        str(backend.get("model") or ""),
        app_cfg,
        base_url_snapshot=str(backend.get("base_url_snapshot") or ""),
    )
    provider_name = str(backend.get("provider_name") or resolution.name)
    provider_revision = str(
        backend.get("provider_revision") or resolution.revision
    )
    judges = [
        judge.model_copy(update={
            "base_url": resolution.base_url,
            "model": resolution.model,
            "api_key_env": None,
            "api_key_value": resolution.api_key,
            "provider_id": resolution.id,
            "provider_name": provider_name,
            "provider_revision": provider_revision,
        })
        for judge in app_cfg.judges
    ]
    eval_options = app_cfg.eval_options.model_copy(update={
        "classify_model": resolution.model,
        "classify_base_url": resolution.base_url,
        "classify_api_key_env": None,
    })
    return app_cfg.model_copy(update={
        "judges": judges,
        "eval_options": eval_options,
    })


def _normalize_eval_options(app_cfg, options: dict) -> tuple[dict, object]:
    """校验前端绑定并保存无密钥的 Provider 快照。"""
    normalized = dict(options or {})
    backend = normalized.get("judge_backend") or {}
    provider_id = str(backend.get("provider_id") or "").strip()
    if not provider_id:
        normalized.pop("judge_backend", None)
        return normalized, app_cfg
    resolution = _llm_provider_store().resolve(
        provider_id,
        str(backend.get("model") or ""),
        app_cfg,
    )
    normalized["judge_backend"] = {
        "provider_id": resolution.id,
        "provider_name": resolution.name,
        "model": resolution.model,
        "base_url_snapshot": resolution.base_url,
        "provider_revision": resolution.revision,
        "builtin": resolution.builtin,
    }
    return normalized, _runtime_config_for_options(app_cfg, normalized)


def _resolve_operation_video_path(raw_path: str) -> Path:
    return resolve_operation_video_path(raw_path, base_dir=BASE_DIR)


def _prepare_operation_item(item: dict) -> dict:
    return prepare_cached_operation_item(
        item,
        base_dir=BASE_DIR,
        runs_dir=RUNS_DIR,
        probe_fn=probe_duration,
        extract_fn=extract_scene_keyframes,
    )


def _validate_eval_request(req: EvalReq, app_cfg) -> None:
    """Reject requests for which every selected judge would be skipped."""
    selected = req.options.get("judges") or (
        [app_cfg.judges[0].name] if app_cfg.judges else []
    )
    selected_judges = [judge for judge in app_cfg.judges if judge.name in selected]
    if not selected_judges:
        selected_judges = app_cfg.judges[:1]
    if req.mode not in ("single", "process") or not selected_judges:
        return
    if not all(judge.persona == "product_expert" for judge in selected_judges):
        return
    missing = [
        index + 1
        for index, item in enumerate(req.items)
        if not str(item.get("competitor") or "").strip()
    ]
    if missing:
        preview = "、".join(map(str, missing[:8]))
        suffix = "…" if len(missing) > 8 else ""
        raise HTTPException(
            422,
            "产品专家需要竞品答案；当前没有其他可用裁判，"
            f"第 {preview}{suffix} 条缺少 competitor。"
            "请补充竞品答案，或同时选择研发人员/终端用户。",
        )


def _terminal_user_judge_name(app_cfg) -> str:
    """返回终端用户裁判的稳定配置名，不依赖裁判排列顺序。"""
    judge = next(
        (candidate for candidate in app_cfg.judges if candidate.persona == "end_user"),
        None,
    )
    if judge is None:
        judge = next(
            (
                candidate
                for candidate in app_cfg.judges
                if str(candidate.display or "").strip() == "终端用户"
            ),
            None,
        )
    if judge is None:
        raise HTTPException(500, "当前配置缺少终端用户裁判")
    return judge.name


def _with_operation_eval_persona(app_cfg, mode: Mode, options: dict) -> dict:
    """任务类固定终端用户视角；模型服务由 judge_backend 独立选择。"""
    normalized = dict(options or {})
    if mode == "operation":
        normalized["judges"] = [_terminal_user_judge_name(app_cfg)]
        normalized.setdefault("concurrency", 8)
    return normalized


def _normalize_single_operation_item(raw_item: dict) -> dict:
    """复用任务类 JSONL 解析规则，校验单题并保留所有原始字段。"""
    item_id = raw_item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise HTTPException(422, "item.id 必须是非空字符串")
    if len(item_id.strip()) > 256:
        raise HTTPException(422, "item.id 不能超过 256 个字符")
    items, errors = parse_jsonl(
        json.dumps(raw_item, ensure_ascii=False),
        "operation",
    )
    if errors:
        raise HTTPException(422, errors[0].replace("第 1 行 ", "item."))
    if len(items) != 1:
        raise HTTPException(422, "item 不是有效的任务类录屏数据")
    return items[0]


def _validate_external_task_id(task_id: str) -> str:
    normalized = task_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", normalized):
        raise HTTPException(
            422,
            "task_id 仅支持 1-128 位字母、数字、下划线和连字符，且首位必须是字母或数字",
        )
    return normalized


def _normalized_dataset_identity(value) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"nan", "none", "null"}:
        return None
    return normalized


def _dataset_item_keys(item: dict) -> set[str]:
    """返回题目的全部稳定标识，兼容表格原始列与标准字段。"""
    source = item.get("source_data")
    source = source if isinstance(source, dict) else {}
    keys: set[str] = set()
    for field in ("index", "序号", "id"):
        for container in (source, item):
            normalized = _normalized_dataset_identity(container.get(field))
            if normalized is not None:
                keys.add(normalized)
    return keys


def _dataset_item_key(item: dict) -> str | None:
    """返回用于提示的主标识；表格优先原始 index/序号。"""
    source = item.get("source_data")
    source = source if isinstance(source, dict) else {}
    for field in ("index", "序号", "id"):
        for container in (source, item):
            normalized = _normalized_dataset_identity(container.get(field))
            if normalized is not None:
                return normalized
    return None


def _normalized_query(value) -> str:
    """Query 冲突只忽略首尾及连续空白，不做语义改写判断。"""
    return " ".join(str(value or "").split())


def _append_preview_value(item: dict, *fields: str):
    """从标准字段或原始表格字段中读取追加预览值。"""
    source = item.get("source_data")
    source = source if isinstance(source, dict) else {}
    for field in fields:
        for container in (item, source):
            value = container.get(field)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def _append_new_item_preview(item: dict, position: int, key: str) -> dict:
    """返回可安全展示的新增题目摘要，避免把整份 source_data 原样回传。"""
    raw_images = _append_preview_value(
        item,
        "query_images",
        "query_image_path",
        "attachment_path",
    )
    if isinstance(raw_images, (list, tuple)):
        query_images = [str(value) for value in raw_images if str(value or "").strip()]
    elif raw_images is None:
        query_images = []
    else:
        query_images = [str(raw_images)]
    return {
        "incoming_position": position,
        "incoming_no": position + 1,
        "key": key,
        "incoming_id": str(item.get("id") or f"第{position + 1}条"),
        "query": str(_append_preview_value(item, "query", "question") or ""),
        "context": str(_append_preview_value(item, "context") or ""),
        "video_path": str(_append_preview_value(item, "video_path") or ""),
        "query_images": query_images,
        "task_start_time": _append_preview_value(item, "task_start_time"),
        "task_end_time": _append_preview_value(item, "task_end_time"),
    }


def _ensure_dataset_tracking(task) -> None:
    """为旧任务补齐一份可维护的基线批次，不改变题目索引。"""
    if task.mode != "operation":
        return
    if (task.options or {}).get("operation_layout") == "multi_group":
        return
    if task.dataset_batches:
        for item in task.items:
            item.setdefault("dataset_status", DATASET_ACTIVE)
            item.setdefault("dataset_revision", 1)
            item.setdefault(
                "dataset_source_batch_id",
                task.dataset_batches[0].get("batch_id") or "batch-legacy",
            )
        return

    batch_id = f"batch-legacy-{uuid.uuid4().hex[:8]}"
    for index, item in enumerate(task.items):
        task.items[index] = tracked_item(item, batch_id=batch_id)
    batch = {
        "batch_id": batch_id,
        "batch_no": 1,
        "kind": "initial",
        "source_dataset_name": task.dataset_name or "历史基线数据集",
        "imported_at": task.created_at,
        "row_count": len(task.items),
        "inserted_count": len(task.items),
        "replaced_count": 0,
        "skipped_count": 0,
        "status": "active",
        "migrated_from_legacy": True,
    }
    try:
        batch["snapshot_path"] = save_dataset_batch_snapshot(
            task,
            batch_id=batch_id,
            batch_no=1,
            kind="initial",
            source_name=batch["source_dataset_name"],
            items=task.items,
        )
    except (OSError, TypeError, ValueError) as exc:
        batch["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    task.dataset_batches.append(batch)


def _assert_dataset_maintenance_available(task) -> None:
    if task.mode != "operation":
        raise HTTPException(422, "目前仅任务类（录屏）支持数据集维护")
    if (task.options or {}).get("operation_layout") == "multi_group":
        raise HTTPException(409, "任务类多组评估暂不支持数据集维护")
    if task.status in {"pending", "running", "rerunning"}:
        raise HTTPException(409, "评估运行期间不能修改数据集")
    if task.execution is not None and not task.execution.done():
        raise HTTPException(409, "当前任务仍有执行中的操作")
    if any(not execution.done() for execution in task.item_executions.values()):
        raise HTTPException(409, "当前任务仍有单题评估在执行")


def _dataset_change(
    task,
    *,
    action: str,
    item_index: int | None = None,
    batch_id: str = "",
    reason: str = "",
    details: dict | None = None,
) -> dict:
    item = (
        task.items[item_index]
        if item_index is not None and 0 <= item_index < len(task.items)
        else {}
    )
    row = {
        "change_id": f"change-{uuid.uuid4().hex[:10]}",
        "changed_at": time.time(),
        "action": action,
        "item_index": item_index,
        "item_key": _dataset_item_key(item) if item else "",
        "item_id": item.get("id") or "",
        "batch_id": batch_id,
        "reason": reason.strip(),
    }
    if details:
        row.update(details)
    task.dataset_change_log.append(row)
    return row


def _dataset_maintenance_payload(task) -> dict:
    active_append_batches = [
        batch for batch in task.dataset_batches
        if batch.get("kind") == "append" and batch.get("status") == "active"
    ]
    latest_rollbackable = (
        active_append_batches[-1].get("batch_id")
        if active_append_batches else None
    )
    batches = []
    for batch in task.dataset_batches:
        batches.append({
            key: value for key, value in batch.items()
            if key not in {"rollback_path"}
        } | {
            "downloadable": bool(dataset_artifact_path(batch.get("snapshot_path") or "")),
            "rollback_allowed": (
                batch.get("batch_id") == latest_rollbackable
                and batch.get("kind") == "append"
            ),
        })
    items = []
    for index, item in enumerate(task.items):
        items.append({
            "item_index": index,
            "item_id": item.get("id") or f"q{index}",
            "item_key": _dataset_item_key(item) or item.get("id") or f"q{index}",
            "query": item.get("query") or item.get("question") or "",
            "dataset_status": item.get("dataset_status") or DATASET_ACTIVE,
            "dataset_revision": int(item.get("dataset_revision") or 1),
            "source_batch_id": item.get("dataset_source_batch_id") or "",
            "evaluation_status": _append_item_evaluation_status(task, index),
        })
    return {
        "task_id": task.id,
        "dataset_name": task.dataset_name,
        "active_count": active_total(task.items),
        "excluded_count": len(task.items) - active_total(task.items),
        "stored_count": len(task.items),
        "items": items,
        "batches": batches,
        "changes": task.dataset_change_log[-500:],
    }


def _append_item_evaluation_status(task, item_index: int) -> str:
    result = next(
        (
            row for row in task.results
            if _web_result_index(row) == item_index
        ),
        None,
    )
    if result is not None:
        return "failed" if result.get("error") else "succeeded"
    progress = task.item_progress.get(str(item_index)) or {}
    return "failed" if progress.get("status") == "error" else "missing"


def _build_append_merge_preview(task, incoming_items: list[dict]) -> dict:
    """构造只读合并计划；新批次内部重复仍视为数据错误。"""
    identity_to_indices: dict[str, set[int]] = {}
    for index, item in enumerate(task.items):
        for identity in _dataset_item_keys(item):
            identity_to_indices.setdefault(identity, set()).add(index)

    seen_incoming: set[str] = set()
    duplicate_labels: list[str] = []
    missing_labels: list[str] = []
    entries: list[dict] = []
    conflicts: list[dict] = []
    new_items: list[dict] = []
    for position, item in enumerate(incoming_items):
        identities = _dataset_item_keys(item)
        key = _dataset_item_key(item)
        label = str(item.get("id") or f"第{position + 1}条")
        if not identities or key is None:
            missing_labels.append(label)
            continue
        if identities & seen_incoming:
            duplicate_labels.append(label)
            continue
        seen_incoming.update(identities)

        matched_indices: set[int] = set()
        for identity in identities:
            matched_indices.update(identity_to_indices.get(identity) or set())
        if len(matched_indices) > 1:
            raise HTTPException(
                409,
                f"历史数据中标识 {key} 同时命中多条记录，无法安全自动合并",
            )
        existing_index = next(iter(matched_indices), None)
        entry = {
            "key": key,
            "incoming_position": position,
            "existing_index": existing_index,
        }
        entries.append(entry)
        if existing_index is None:
            new_items.append(_append_new_item_preview(item, position, key))
            continue

        existing_item = task.items[existing_index]
        existing_query = str(
            existing_item.get("query") or existing_item.get("question") or ""
        )
        incoming_query = str(item.get("query") or item.get("question") or "")
        query_match = _normalized_query(existing_query) == _normalized_query(incoming_query)
        evaluation_status = _append_item_evaluation_status(task, existing_index)
        dataset_status = str(existing_item.get("dataset_status") or DATASET_ACTIVE)
        recommended_action = None
        recommendation_reason = "Query 不一致，需要人工确认保留哪一条"
        if query_match and dataset_status == DATASET_EXCLUDED:
            recommended_action = "replace_and_rerun"
            recommendation_reason = "旧题已被排除，建议使用新数据恢复并重新评估"
        elif query_match and evaluation_status in {"failed", "missing"}:
            recommended_action = "replace_and_rerun"
            recommendation_reason = "旧数据评测调用失败或没有结果，建议使用新数据重跑"
        elif query_match:
            recommended_action = "keep_existing"
            recommendation_reason = "旧数据已经正常产出评测结果，建议保留并跳过新数据"
        conflict = {
            **entry,
            "existing_dataset_index": existing_index + 1,
            "existing_id": existing_item.get("id") or f"q{existing_index}",
            "incoming_id": item.get("id") or f"第{position + 1}条",
            "existing_query": existing_query,
            "incoming_query": incoming_query,
            "query_match": query_match,
            "existing_evaluation_status": evaluation_status,
            "existing_dataset_status": dataset_status,
            "recommended_action": recommended_action,
            "recommendation_reason": recommendation_reason,
        }
        conflicts.append(conflict)

    if missing_labels:
        raise HTTPException(
            422,
            "追加数据缺少稳定题目标识（index、序号或 id）："
            + "、".join(missing_labels[:10]),
        )
    if duplicate_labels:
        raise HTTPException(
            409,
            "新增数据集内部包含重复题目标识："
            + "、".join(duplicate_labels[:10])
            + (" 等" if len(duplicate_labels) > 10 else ""),
        )

    return {
        "incoming_total": len(incoming_items),
        "new_count": len(entries) - len(conflicts),
        "conflict_count": len(conflicts),
        "recommended_keep_count": sum(
            row["recommended_action"] == "keep_existing" for row in conflicts
        ),
        "recommended_replace_count": sum(
            row["recommended_action"] == "replace_and_rerun" for row in conflicts
        ),
        "unresolved_count": sum(
            row["recommended_action"] is None for row in conflicts
        ),
        "new_items": new_items,
        "conflicts": conflicts,
        "_entries": entries,
    }


def _web_result_index(result: dict) -> int | None:
    try:
        return int(result.get("index"))
    except (TypeError, ValueError):
        return None


def _single_item_evaluation_status(task, item_index: int) -> str:
    result = next(
        (
            row
            for row in task.results
            if _web_result_index(row) == item_index
        ),
        None,
    )
    if result is not None:
        return "failed" if result.get("error") else "succeeded"

    progress = (
        task.item_progress.get(str(item_index))
        or task.item_progress.get(item_index)
        or {}
    )
    progress_status = str(progress.get("status") or "")
    if progress_status == "error":
        return "failed"
    if progress_status == "cancelled" or task.status == "cancelled":
        return "cancelled"
    if progress_status == "running":
        return "running"
    if task.status == "error":
        return "failed"
    return "pending"


def _single_eval_concurrency() -> int:
    """读取单题接口的数据集并发上限，非法值回退为 15。"""
    try:
        value = int(os.getenv("AUTO_EVAL_SINGLE_CONCURRENCY", "15"))
    except (TypeError, ValueError):
        value = 15
    return max(1, min(value, 100))


def _sse(event: str, data, *, event_id: int | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/config")
def api_config():
    c = cfg()
    return {
        "judges": [
            {
                "name": j.name,
                "display": j.display or j.name,
                "persona": j.persona,
                "enable_web_search": j.enable_web_search,
                "base_url": j.base_url or "",
                "model": j.model,
            }
            for j in c.judges
        ],
        "models": [m.name for m in c.models],
        "rubrics": [d.name for d in c.rubrics],
        "scale": c.rubrics[0].scale if c.rubrics else 5,
    }


@app.get("/api/llm-providers")
def api_llm_providers():
    return {"items": _llm_provider_store().list_public(cfg())}


@app.post("/api/llm-providers", status_code=201)
def api_llm_provider_create(payload: LLMProviderPayload):
    try:
        provider = _llm_provider_store().create(payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"provider": provider}


@app.put("/api/llm-providers/{provider_id}")
def api_llm_provider_update(provider_id: str, payload: LLMProviderPayload):
    try:
        provider = _llm_provider_store().update(provider_id, payload)
    except KeyError as exc:
        raise HTTPException(404, "Provider not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"provider": provider}


@app.delete("/api/llm-providers/{provider_id}")
def api_llm_provider_delete(provider_id: str):
    try:
        deleted = _llm_provider_store().delete(provider_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "Provider not found")
    return {"ok": True}


@app.post("/api/llm-providers/{provider_id}/test")
async def api_llm_provider_test(provider_id: str, req: ProviderTestReq):
    try:
        provider = _llm_provider_store().resolve(provider_id, req.model, cfg())
    except KeyError as exc:
        raise HTTPException(404, "Provider not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    started = time.perf_counter()
    client = None
    try:
        client = build_openai_client(
            base_url=provider.base_url,
            api_key=provider.api_key,
            connect_timeout_s=10,
            read_timeout_s=30,
        )
        response = await stream_chat_completion(
            client,
            {
                "model": provider.model,
                "messages": [{"role": "user", "content": "请只回复 OK"}],
                "temperature": 0,
                "max_tokens": 16,
            },
            include_usage=False,
            total_timeout_s=30,
            max_attempts=1,
        )
        answer = str(response.choices[0].message.content or "").strip()
    except Exception as exc:
        message = str(exc).replace(provider.api_key, "***")
        raise HTTPException(
            502,
            f"{type(exc).__name__}: {message[:2000]}",
        ) from exc
    finally:
        if client is not None:
            await client.close()
    return {
        "ok": True,
        "provider_id": provider.id,
        "model": provider.model,
        "latency_s": round(time.perf_counter() - started, 3),
        "response": answer[:500],
    }


@app.get("/api/knowledge/operation")
def api_operation_knowledge():
    store = _operation_knowledge_store()
    published = store.published()
    draft = store.draft()
    effective = draft or published
    return {
        "published": published.model_dump(mode="json"),
        "draft": effective.model_dump(mode="json"),
        "has_unpublished_changes": draft is not None,
        "prompt_preview": render_expert_knowledge(effective),
    }


@app.put("/api/knowledge/operation/draft")
def api_save_operation_knowledge_draft(knowledge: ExpertKnowledgeBase):
    saved = _operation_knowledge_store().save_draft(knowledge)
    return {
        "ok": True,
        "draft": saved.model_dump(mode="json"),
        "prompt_preview": render_expert_knowledge(saved),
    }


@app.delete("/api/knowledge/operation/draft")
def api_discard_operation_knowledge_draft():
    store = _operation_knowledge_store()
    store.discard_draft()
    published = store.published()
    return {
        "ok": True,
        "draft": published.model_dump(mode="json"),
        "prompt_preview": render_expert_knowledge(published),
    }


@app.post("/api/knowledge/operation/publish")
def api_publish_operation_knowledge():
    try:
        published = _operation_knowledge_store().publish()
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    # 新任务读取新版本；已经启动的任务仍持有原 AppConfig，保证单次批跑可复现。
    _state["cfg"] = load_config(CONFIG_DIR)
    return {
        "ok": True,
        "published": published.model_dump(mode="json"),
        "prompt_preview": render_expert_knowledge(published),
    }


@app.post("/api/parse")
def api_parse(req: ParseReq):
    if req.jsonl:
        items, errs = parse_jsonl(req.jsonl, req.mode)
    elif req.text is not None:
        items, errs = parse_text(req.text, req.mode)
    else:
        raise HTTPException(400, "需提供 text 或 jsonl")
    return {"items": items, "errors": errs, "count": len(items)}


_TABLE_DATASET_SUFFIXES = {".csv", ".xlsx", ".xls", ".xlsm"}
_MAX_TABLE_DATASET_BYTES = 100 * 1024 * 1024


@app.post("/api/operation/import-table")
async def api_import_operation_table(file: UploadFile = File(...)):
    """把 CSV/Excel 转为与任务类 JSONL 相同的标准题目。"""
    filename = Path(file.filename or "dataset").name
    suffix = Path(filename).suffix.lower()
    if suffix not in _TABLE_DATASET_SUFFIXES:
        supported = "、".join(sorted(_TABLE_DATASET_SUFFIXES))
        raise HTTPException(422, f"不支持的表格格式；请选择 {supported}")
    content = await file.read(_MAX_TABLE_DATASET_BYTES + 1)
    if len(content) > _MAX_TABLE_DATASET_BYTES:
        raise HTTPException(413, "表格文件超过 100MB 限制")
    if not content:
        raise HTTPException(422, "表格文件为空")

    try:
        with tempfile.TemporaryDirectory(prefix="auto_eval_table_") as temp_dir:
            temp_root = Path(temp_dir)
            input_path = temp_root / f"input{suffix}"
            output_path = temp_root / "output.jsonl"
            input_path.write_bytes(content)
            result = convert_table(
                input_path,
                input_prefix=Path(filename).stem,
                output_path=output_path,
                sheet="auto",
                project_root=BASE_DIR,
            )
            jsonl = "\n".join(
                json.dumps(row, ensure_ascii=False, allow_nan=False)
                for row in result.rows
            )
    except (FileNotFoundError, ImportError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    items, errors = parse_jsonl(jsonl, "operation")
    return {
        "items": items,
        "errors": errors,
        "count": len(items),
        "jsonl": jsonl,
        "warnings": result.warnings,
        "summary": {
            "filename": filename,
            "format": suffix.lstrip(".").upper(),
            "sheet": result.selected_sheet,
            "source_rows": len(result.rows),
            "imported_rows": len(items),
            "warning_rows": len(result.warnings),
            "missing_video_rows": len(result.missing_video_ids),
            "ignored_empty_rows": result.ignored_empty_rows,
            "direct_standard_columns": list(result.direct_standard_columns),
        },
    }


@app.post("/api/operation/comparison/import")
async def api_import_operation_comparison(file: UploadFile = File(...)):
    """导入已评估的任务类结果集，供独立对比分析使用。"""
    filename = Path(file.filename or "comparison_results").name
    suffix = Path(filename).suffix.lower()
    if suffix not in COMPARISON_IMPORT_SUFFIXES:
        raise HTTPException(422, "仅支持 JSONL、CSV、XLSX、XLS、XLSM")
    content = await file.read(_MAX_TABLE_DATASET_BYTES + 1)
    if len(content) > _MAX_TABLE_DATASET_BYTES:
        raise HTTPException(413, "评估结果文件超过 100MB 限制")
    try:
        return import_operation_comparison_file(filename, content)
    except (ImportError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/operation/groups/align")
def api_align_operation_groups(req: OperationGroupsAlignReq):
    if len(req.groups) < 2:
        raise HTTPException(422, "任务类多组评估至少需要两个实验组")
    group_ids = [group.group_id.strip() for group in req.groups]
    if any(not group_id for group_id in group_ids):
        raise HTTPException(422, "group_id 不能为空")
    if len(set(group_ids)) != len(group_ids):
        raise HTTPException(422, "group_id 不能重复")
    group_names = [group.group_name.strip() for group in req.groups]
    if any(not group_name for group_name in group_names):
        raise HTTPException(422, "实验组名称不能为空")
    if len(set(group_names)) != len(group_names):
        raise HTTPException(422, "实验组名称不能重复")
    invalid_roles = [
        group.group_role for group in req.groups
        if group.group_role not in {"control", "experiment"}
    ]
    if invalid_roles:
        raise HTTPException(422, "数据组角色只能是 control 或 experiment")
    parsed_groups: list[dict] = []
    parse_errors: list[str] = []
    for group in req.groups:
        items, errors = parse_jsonl(group.jsonl, "operation")
        parse_errors.extend(
            f"{group.group_name}：{error}" for error in errors
        )
        parsed_groups.append({
            "group_id": group.group_id,
            "group_name": group.group_name,
            "group_role": group.group_role,
            "dataset_name": group.dataset_name,
            "items": items,
        })
    aligned = align_operation_groups(parsed_groups)
    aligned["errors"] = [*parse_errors, *aligned["errors"]]
    return aligned


@app.post("/api/eval")
async def api_eval(req: EvalReq):
    if not req.items:
        raise HTTPException(400, "items 为空")
    app_cfg = cfg()
    append_task = None
    if req.submit_mode == "append":
        if req.mode != "operation":
            raise HTTPException(422, "目前仅任务类（录屏）支持追加评估")
        if not req.task_id.strip():
            raise HTTPException(422, "追加评估必须指定 task_id")
        task_id = _validate_external_task_id(req.task_id)
        append_task = get_task(task_id)
        if append_task is None:
            raise HTTPException(404, "目标历史评估集不存在")
        if append_task.mode != req.mode:
            raise HTTPException(409, "目标历史评估集的评测模式不一致")
        if (append_task.options or {}).get("operation_layout") == "multi_group":
            raise HTTPException(409, "任务类多组评估暂不支持分段追加")
        if append_task.status in {"pending", "running", "rerunning"}:
            raise HTTPException(409, "目标历史评估集正在运行，请完成或中断后再追加")
        if append_task.execution is not None and not append_task.execution.done():
            raise HTTPException(409, "目标历史评估集已有执行中的操作")
        if any(
            not execution.done()
            for execution in append_task.item_executions.values()
        ):
            raise HTTPException(409, "目标历史评估集仍有单题任务在执行")
        append_options = dict(append_task.options or {})
        for key in ("concurrency", "eval_timeout_s", "eval_timeout"):
            if key in req.options:
                append_options[key] = req.options[key]
        requested_options = _with_operation_eval_persona(
            app_cfg,
            req.mode,
            append_options,
        )
    else:
        if req.task_id.strip():
            raise HTTPException(422, "新建评估不能指定 task_id")
        requested_options = _with_operation_eval_persona(
            app_cfg,
            req.mode,
            req.options,
        )
    _validate_eval_request(
        req.model_copy(update={"options": requested_options}),
        app_cfg,
    )
    try:
        task_options, runtime_cfg = _normalize_eval_options(
            app_cfg,
            requested_options,
        )
    except KeyError as exc:
        raise HTTPException(422, f"Provider 不存在：{exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if append_task is None:
        task = new_task(
            req.mode,
            req.items,
            task_options,
            dataset_name=req.dataset_name.strip(),
        )

        async def _start_later():
            # 先把 task_id 响应给前端，再启动可能较重的评估任务；
            # 避免后台裁判/工具调用抢占事件循环，导致 /api/eval 本身迟迟不返回。
            await asyncio.sleep(0.05)
            await run_eval(task, runtime_cfg)

        execution = asyncio.create_task(_start_later())
        action = "created"
        item_indices = list(range(len(task.items)))
    else:
        task = append_task
        merge_preview = _build_append_merge_preview(task, req.items)
        public_preview = {
            key: value
            for key, value in merge_preview.items()
            if not key.startswith("_")
        }
        if req.conflict_policy == "preview":
            return {
                "task_id": task.id,
                "action": "preview",
                "dataset_size": active_total(task.items),
                "merge_preview": public_preview,
            }

        conflicts = merge_preview["conflicts"]
        if req.conflict_policy == "reject" and conflicts:
            labels = [str(row["key"]) for row in conflicts]
            raise HTTPException(
                409,
                "追加数据包含历史重复题目："
                + "、".join(labels[:10])
                + (" 等" if len(labels) > 10 else ""),
            )
        resolutions: dict[str, str] = {}
        if req.conflict_policy == "smart":
            unresolved = [
                row for row in conflicts if row["recommended_action"] is None
            ]
            if unresolved:
                labels = "、".join(str(row["key"]) for row in unresolved[:10])
                raise HTTPException(
                    409,
                    f"以下重复题目的 Query 不一致，需要人工选择：{labels}",
                )
            resolutions = {
                str(row["key"]): str(row["recommended_action"])
                for row in conflicts
            }
        elif req.conflict_policy == "resolve":
            missing_resolutions = [
                str(row["key"])
                for row in conflicts
                if str(row["key"]) not in req.conflict_resolutions
            ]
            if missing_resolutions:
                raise HTTPException(
                    422,
                    "以下重复题目尚未选择保留策略："
                    + "、".join(missing_resolutions[:10]),
                )
            resolutions = dict(req.conflict_resolutions)

        _ensure_dataset_tracking(task)
        batch_no = max(
            [int(batch.get("batch_no") or 0) for batch in task.dataset_batches],
            default=0,
        ) + 1
        segment_no = batch_no
        source_dataset_name = req.dataset_name.strip() or f"追加批次{segment_no}"
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        append_id = f"append-{uuid.uuid4().hex[:8]}"
        previous_state = {
            "items": list(task.items),
            "results": list(task.results),
            "item_progress": dict(task.item_progress),
            "progress_events": dict(task.progress_events),
            "done_total": task.done_total,
            "summary": task.summary,
            "options": task.options,
            "status": task.status,
            "error": task.error,
            "finished_at": task.finished_at,
            "dataset_batches": list(task.dataset_batches),
            "dataset_change_log": list(task.dataset_change_log),
        }
        item_indices: list[int] = []
        inserted_indices: list[int] = []
        replaced_indices: list[int] = []
        skipped_keys: list[str] = []
        replacement_backups: list[dict] = []
        for entry in merge_preview["_entries"]:
            position = int(entry["incoming_position"])
            existing_index = entry["existing_index"]
            previous_revision = (
                int(task.items[existing_index].get("dataset_revision") or 1)
                if existing_index is not None else 0
            )
            incoming = tracked_item(
                {
                    **req.items[position],
                    "evaluation_segment_no": segment_no,
                    "evaluation_source_dataset": source_dataset_name,
                },
                batch_id=batch_id,
                revision=previous_revision + 1,
            )
            if existing_index is None:
                new_index = len(task.items)
                task.items.append(incoming)
                inserted_indices.append(new_index)
                item_indices.append(new_index)
                _dataset_change(
                    task,
                    action="add",
                    item_index=new_index,
                    batch_id=batch_id,
                )
                continue
            action = resolutions.get(str(entry["key"]), "keep_existing")
            if action == "keep_existing":
                skipped_keys.append(str(entry["key"]))
                continue
            previous_result = next(
                (
                    deepcopy(row) for row in task.results
                    if _web_result_index(row) == existing_index
                ),
                None,
            )
            replacement_backups.append({
                "item_index": existing_index,
                "before_item": deepcopy(task.items[existing_index]),
                "before_result": previous_result,
                "before_progress": deepcopy(
                    task.item_progress.get(str(existing_index))
                    or task.item_progress.get(existing_index)
                ),
                "before_progress_events": deepcopy(
                    task.progress_events.get(str(existing_index))
                    or task.progress_events.get(existing_index)
                    or []
                ),
            })
            task.items[existing_index] = incoming
            replaced_indices.append(existing_index)
            item_indices.append(existing_index)
            _dataset_change(
                task,
                action="replace",
                item_index=existing_index,
                batch_id=batch_id,
                details={
                    "before_revision": previous_revision,
                    "after_revision": previous_revision + 1,
                },
            )

        replaced_set = set(replaced_indices)
        if replaced_set:
            task.results = [
                row for row in task.results
                if _web_result_index(row) not in replaced_set
            ]
            for index in replaced_set:
                task.item_progress.pop(str(index), None)
                task.item_progress.pop(index, None)
                task.progress_events.pop(str(index), None)
                task.progress_events.pop(index, None)
            task.done_total = active_result_count(task.items, task.results)

        merge_summary = {
            "incoming_count": len(req.items),
            "inserted_count": len(inserted_indices),
            "replaced_count": len(replaced_indices),
            "skipped_count": len(skipped_keys),
            "conflict_count": len(conflicts),
        }
        batch_record = {
            "batch_id": batch_id,
            "batch_no": batch_no,
            "kind": "append",
            "append_id": append_id,
            "source_dataset_name": source_dataset_name,
            "imported_at": time.time(),
            "row_count": len(req.items),
            "inserted_count": len(inserted_indices),
            "replaced_count": len(replaced_indices),
            "skipped_count": len(skipped_keys),
            "inserted_item_indices": inserted_indices,
            "replaced_item_indices": replaced_indices,
            "skipped_item_keys": skipped_keys,
            "status": "active",
            "base_status": previous_state["status"],
            "base_error": previous_state["error"],
        }
        artifact_references: list[str] = []
        try:
            batch_record["snapshot_path"] = save_dataset_batch_snapshot(
                task,
                batch_id=batch_id,
                batch_no=batch_no,
                kind="append",
                source_name=source_dataset_name,
                items=req.items,
            )
            artifact_references.append(batch_record["snapshot_path"])
            if inserted_indices or replacement_backups:
                batch_record["rollback_path"] = save_dataset_rollback_snapshot(
                    task,
                    batch_id=batch_id,
                    payload={
                        "schema_version": 1,
                        "task_id": task.id,
                        "batch_id": batch_id,
                        "inserted_item_indices": inserted_indices,
                        "replacements": replacement_backups,
                    },
                )
                artifact_references.append(batch_record["rollback_path"])
        except (OSError, TypeError, ValueError) as exc:
            task.items = previous_state["items"]
            task.results = previous_state["results"]
            task.item_progress = previous_state["item_progress"]
            task.progress_events = previous_state["progress_events"]
            task.done_total = previous_state["done_total"]
            task.dataset_batches = previous_state["dataset_batches"]
            task.dataset_change_log = previous_state["dataset_change_log"]
            raise HTTPException(
                500,
                f"追加数据归档失败，未修改历史评估集：{type(exc).__name__}: {exc}",
            ) from exc
        task.dataset_batches.append(batch_record)
        if not item_indices:
            finished_at = datetime.now().timestamp()
            completed_attempt = {
                "append_id": append_id,
                "segment_no": segment_no,
                "source_dataset_name": source_dataset_name,
                "batch_id": batch_id,
                "item_indices": [],
                "inserted_item_indices": [],
                "replaced_item_indices": [],
                "skipped_item_keys": skipped_keys,
                "total": 0,
                "done": 0,
                "status": "done",
                "started_at": finished_at,
                "finished_at": finished_at,
                "duration_s": 0.0,
                **merge_summary,
            }
            task.append_history.append(completed_attempt)
            if not save_task(task):
                task.append_history.pop()
                task.dataset_batches = previous_state["dataset_batches"]
                task.dataset_change_log = previous_state["dataset_change_log"]
                for reference in artifact_references:
                    path = dataset_artifact_path(reference)
                    if path:
                        path.unlink(missing_ok=True)
                raise HTTPException(500, "追加记录写入历史快照失败")
            return {
                "task_id": task.id,
                "action": "skipped",
                "item_indices": [],
                "dataset_size": active_total(task.items),
                "append_id": append_id,
                "merge_summary": merge_summary,
            }

        task.options = task_options
        task.status = "running"
        task.error = None
        task.finished_at = None
        task.summary = {}
        task.active_append = {
            "append_id": append_id,
            "batch_id": batch_id,
            "segment_no": segment_no,
            "source_dataset_name": source_dataset_name,
            "item_indices": item_indices,
            "inserted_item_indices": inserted_indices,
            "replaced_item_indices": replaced_indices,
            "skipped_item_keys": skipped_keys,
            "total": len(item_indices),
            "done": 0,
            "status": "starting",
            "started_at": datetime.now().timestamp(),
            "base_duration_s": float(task.duration_s or 0.0),
            "base_status": previous_state["status"],
            "base_error": previous_state["error"],
            "judge_backend": dict(task_options.get("judge_backend") or {}),
            "concurrency": task_options.get("concurrency"),
            "eval_timeout_s": (
                task_options.get("eval_timeout_s")
                or task_options.get("eval_timeout")
            ),
            **merge_summary,
        }
        if not save_task(task):
            task.items = previous_state["items"]
            task.results = previous_state["results"]
            task.item_progress = previous_state["item_progress"]
            task.progress_events = previous_state["progress_events"]
            task.done_total = previous_state["done_total"]
            task.summary = previous_state["summary"]
            task.active_append = None
            task.options = previous_state["options"]
            task.status = previous_state["status"]
            task.error = previous_state["error"]
            task.finished_at = previous_state["finished_at"]
            task.dataset_batches = previous_state["dataset_batches"]
            task.dataset_change_log = previous_state["dataset_change_log"]
            for reference in artifact_references:
                path = dataset_artifact_path(reference)
                if path:
                    path.unlink(missing_ok=True)
            raise HTTPException(500, "追加数据写入历史快照失败，未启动评估")
        execution = asyncio.create_task(run_append(task, runtime_cfg, item_indices))
        action = "appended"
    task.execution = execution

    def clear_execution(finished: asyncio.Task) -> None:
        if task.execution is finished:
            task.execution = None

    execution.add_done_callback(clear_execution)
    return {
        "task_id": task.id,
        "action": action,
        "item_indices": item_indices,
        "dataset_size": active_total(task.items),
        "append_id": (task.active_append or {}).get("append_id"),
        "merge_summary": merge_summary if append_task is not None else None,
    }


@app.post("/api/eval/single", status_code=202)
async def api_eval_single(req: SingleEvalReq):
    """向任务类数据集新增或覆盖一题，并且只评估这一个题目。"""
    task_id = _validate_external_task_id(req.task_id)
    item = _normalize_single_operation_item(req.item)
    app_cfg = cfg()
    judge_name = _terminal_user_judge_name(app_cfg)
    single_concurrency = _single_eval_concurrency()
    dataset_name = req.dataset_name.strip()
    task = get_task(task_id)
    rerun_attempt = None
    if task is None:
        task = new_task(
            "operation",
            [item],
            {
                "judges": [judge_name],
                "concurrency": single_concurrency,
                "submission_source": "single_api",
            },
            dataset_name=dataset_name or task_id,
            task_id=task_id,
        )
        item_index = 0
        action = "created"
    else:
        if task.mode != "operation":
            raise HTTPException(409, "task_id 已属于其他评测模式，不能作为任务类数据集写入")
        if task.execution is not None and not task.execution.done():
            raise HTTPException(409, "当前数据集正在执行 Web 批跑或重跑，暂不能接口写入")
        if (
            task.status in {"pending", "running", "rerunning"}
            and not task.item_executions
        ):
            raise HTTPException(409, "当前数据集存在非接口评估任务，暂不能接口写入")

        item_id = item["id"]
        active = task.item_executions.get(item_id)
        if active is not None and not active.done():
            raise HTTPException(409, f"当前数据集的 {item_id} 仍在评估中，请完成后再提交")

        item_index = next(
            (
                index
                for index, existing in enumerate(task.items)
                if str(existing.get("id") or "") == item_id
            ),
            None,
        )
        if item_index is None:
            task.items.append(item)
            item_index = len(task.items) - 1
            action = "appended"
        else:
            previous_result = next(
                (
                    result
                    for result in task.results
                    if _web_result_index(result) == item_index
                ),
                None,
            )
            task.results = [
                result
                for result in task.results
                if _web_result_index(result) != item_index
            ]
            task.done_total = len({
                index
                for result in task.results
                if (index := _web_result_index(result)) is not None
            })
            task.items[item_index] = item
            action = "overwritten"
            attempt_numbers = [
                int(attempt.get("attempt_no") or 0)
                for attempt in [
                    *task.rerun_history,
                    *task.single_api_attempts.values(),
                ]
            ]
            attempt_no = max(attempt_numbers, default=0) + 1
            rerun_attempt = {
                "attempt_id": f"rerun-{attempt_no}-{uuid.uuid4().hex[:8]}",
                "attempt_no": attempt_no,
                "item_indices": [item_index],
                "total": 1,
                "done": 0,
                "status": "running",
                "base_status": (
                    task.status
                    if task.status in {"done", "error", "cancelled"}
                    else "done"
                ),
                "started_at": datetime.now().timestamp(),
                "items": [],
                "_previous_result": previous_result,
            }
            task.single_api_attempts[item_id] = rerun_attempt

    # 接口和 Web 任务类统一使用终端用户裁判；其余已有运行参数保持不变。
    task.options = {
        **task.options,
        "judges": [judge_name],
        "concurrency": single_concurrency,
        "submission_source": "single_api",
    }
    try:
        runtime_cfg = _runtime_config_for_options(app_cfg, task.options)
    except KeyError as exc:
        raise HTTPException(422, f"Provider 不存在：{exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if dataset_name:
        task.dataset_name = dataset_name
    task.status = "running"
    task.error = None
    if not save_task(task):
        raise HTTPException(500, "单题已写入内存，但历史快照保存失败")

    item_id = item["id"]
    execution = asyncio.create_task(
        run_single_api_item(
            task,
            runtime_cfg,
            item_index,
            item_id,
            rerun=rerun_attempt,
        ),
    )
    task.item_executions[item_id] = execution
    return {
        "task_id": task.id,
        "id": item_id,
        "status": "success",
        "evaluation_status": task.status,
        "action": action,
        "dataset_size": active_total(task.items),
    }


@app.get("/api/eval/single")
def api_eval_single_result(task_id: str, id: str, event: int = 0):
    """查询一题的最新任务类结果，并按需返回 Web 日志进度。"""
    if event < -1:
        raise HTTPException(422, "event 必须为 -1、0 或正整数")

    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "task or item not found")
    if task.mode != "operation":
        raise HTTPException(409, "task_id 不属于任务类（录屏）数据集")

    item_index = next(
        (
            index
            for index, item in enumerate(task.items)
            if str(item.get("id") or "") == id
        ),
        None,
    )
    if item_index is None:
        raise HTTPException(404, "task or item not found")

    response = {
        "task_id": task.id,
        "dataset_name": task.dataset_name,
        "id": id,
        "evaluation_status": _single_item_evaluation_status(task, item_index),
        "result": operation_item_result_row(
            task_to_snapshot(task),
            item_index,
        ),
    }
    if event != 0:
        events = list(
            task.progress_events.get(str(item_index))
            or task.progress_events.get(item_index)
            or []
        )
        response["progress_events"] = events if event == -1 else events[:event]
    return response


@app.post("/api/operation/prepare")
async def api_prepare_operation(req: OperationPrepareReq):
    """批量校验 JSONL 中的本地视频路径并并发抽帧，逐条隔离错误。"""
    if not req.items:
        raise HTTPException(400, "items 为空")
    concurrency = max(1, min(int(req.concurrency or 2), 8))
    semaphore = asyncio.Semaphore(concurrency)

    async def prepare_one(index: int, item: dict) -> tuple[int, dict | None, str | None]:
        async with semaphore:
            try:
                prepared = await asyncio.to_thread(_prepare_operation_item, item)
                return index, prepared, None
            except Exception as exc:
                line = item.get("source_line") or index + 1
                item_id = item.get("id") or f"第 {line} 行"
                return index, None, f"{item_id}：{exc}"

    prepared_rows = await asyncio.gather(*[
        prepare_one(index, item) for index, item in enumerate(req.items)
    ])
    prepared_items: list[dict] = []
    errors: list[str] = []
    for _, item, error in sorted(prepared_rows, key=lambda row: row[0]):
        if item is not None:
            prepared_items.append(item)
        if error:
            errors.append(error)
    return {
        "items": prepared_items,
        "errors": errors,
        "count": len(prepared_items),
        "failed": len(errors),
    }

@app.post("/api/upload/video")
async def api_upload_video(file: UploadFile = File(...), mode: Mode = "operation"):
    """上传视觉评估录屏；富内容模式延迟到开始评估时使用专用参数抽帧。"""
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "视频过大，限制 ≤20MB")
    video_dir = RUNS_DIR / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "v.mp4").suffix.lower() or ".mp4"
    video_path = video_dir / f"{video_id}{suffix}"
    video_path.write_bytes(data)
    duration = probe_duration(video_path)
    if mode == "rich_content":
        return {
            "video_id": video_id,
            "video_path": str(video_path),
            "frames": [],
            "frame_count": 0,
            "duration": round(duration, 2),
        }
    frame_dir = video_dir / f"{video_id}_frames"
    frames = extract_scene_keyframes(video_path, frame_dir)
    return {
        "video_id": video_id,
        "video_path": str(video_path),
        "frames": [str(f) for f in frames],
        "frame_count": len(frames),
        "duration": round(duration, 2),
    }


@app.post("/api/upload/query-image")
async def api_upload_query_image(file: UploadFile = File(...)):
    """上传一张随 Query 提供的原始用户图片；模型调用时才编码为 data URL。"""
    suffix = Path(file.filename or "query-image").suffix.lower()
    if suffix not in QUERY_IMAGE_EXTENSIONS:
        supported = "、".join(sorted(QUERY_IMAGE_EXTENSIONS))
        raise HTTPException(422, f"不支持的图片格式；请选择 {supported}")
    data = await file.read(MAX_QUERY_IMAGE_BYTES + 1)
    if len(data) > MAX_QUERY_IMAGE_BYTES:
        raise HTTPException(413, "用户输入图片超过 10MB 限制")
    if not data:
        raise HTTPException(422, "用户输入图片为空")
    image_dir = RUNS_DIR / "uploads" / "query_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / f"{uuid.uuid4().hex[:16]}{suffix}"
    image_path.write_bytes(data)
    try:
        resolved = resolve_operation_query_image_path(
            str(image_path),
            base_dir=BASE_DIR,
        )
        from PIL import Image

        with Image.open(resolved) as image:
            width, height = image.size
    except ValueError as exc:
        image_path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    return {
        "query_image_path": str(resolved),
        "filename": Path(file.filename or resolved.name).name,
        "width": width,
        "height": height,
        "size": len(data),
    }


@app.get("/api/eval/{task_id}/stream")
async def api_stream(
    task_id: str,
    after: int | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    event_queue = task.subscribe()

    header_cursor: int | None = None
    try:
        header_cursor = int(last_event_id) if last_event_id else None
    except (TypeError, ValueError):
        header_cursor = None
    cursors = [cursor for cursor in (after, header_cursor) if cursor is not None]
    replay_after = max(cursors) if cursors else None

    async def event_gen():
        try:
            # 先回放任务级状态，新标签页无需等待下一条结果即可
            # 显示“评估中 done/total”。
            yield _sse(
                "task_state",
                {
                    "status": task.status,
                    "progress": active_result_count(task.items, task.results),
                    "total": active_total(task.items),
                    "started_at": task.started_at,
                    "finished_at": task.finished_at,
                    "duration_s": task.elapsed_s(),
                    "error": task.error,
                    "active_rerun": task.active_rerun,
                    "active_append": task.active_append,
                    "run_kind": (
                        "rerun" if task.status == "rerunning"
                        else "append" if task.active_append
                        else "initial"
                    ),
                    "rerun_progress": (task.active_rerun or {}).get("done"),
                    "rerun_total": (task.active_rerun or {}).get("total"),
                },
            )
            delivered_cursor = max(0, replay_after or 0)
            terminal_replayed = False
            if replay_after is None:
                # 兼容旧客户端：没有事件游标时仍回放完整页面状态。
                for item_events in list(task.progress_events.values()):
                    for progress_event in item_events:
                        progress_index = _web_result_index(progress_event)
                        if (
                            progress_index is not None
                            and 0 <= progress_index < len(task.items)
                            and not is_item_active(task.items[progress_index])
                        ):
                            continue
                        yield _sse("progress_event", progress_event)
                for progress_item in list(task.item_progress.values()):
                    progress_index = _web_result_index(progress_item)
                    if (
                        progress_index is not None
                        and 0 <= progress_index < len(task.items)
                        and not is_item_active(task.items[progress_index])
                    ):
                        continue
                    yield _sse("item_progress", progress_item)
                for result in list(task.results):
                    result_index = _web_result_index(result)
                    if (
                        result_index is not None
                        and 0 <= result_index < len(task.items)
                        and not is_item_active(task.items[result_index])
                    ):
                        continue
                    yield _sse(
                        "result",
                        {
                            "progress": active_result_count(task.items, task.results),
                            "total": active_total(task.items),
                            "result": result,
                        },
                    )
            else:
                # 新页面先加载快照，SSE 只补发快照游标之后的增量事件。
                # 先订阅再复制日志，队列中的重复事件由 cursor 去重。
                for message in list(task.event_log):
                    cursor = int(message.get("cursor") or 0)
                    if cursor <= delivered_cursor:
                        continue
                    yield _sse(
                        message["event"],
                        message["data"],
                        event_id=cursor,
                    )
                    delivered_cursor = cursor
                    terminal_replayed = message["event"] in {
                        "done", "error", "cancelled", "rerun_done", "rerun_cancelled",
                    }

            if task.status in {"done", "error", "cancelled"}:
                if not terminal_replayed:
                    if task.status == "done":
                        yield _sse(
                            "done",
                            {
                                "summary": task.summary,
                                "total": active_total(task.items),
                                "duration_s": task.duration_s,
                            },
                            event_id=task.event_cursor or None,
                        )
                    elif task.status == "error":
                        yield _sse(
                            "error",
                            {"message": task.error, "duration_s": task.duration_s},
                            event_id=task.event_cursor or None,
                        )
                    else:
                        yield _sse(
                            "cancelled",
                            {
                                "message": task.error or "任务已中断",
                                "duration_s": task.duration_s,
                            },
                            event_id=task.event_cursor or None,
                        )
                return
            # 每个 SSE 连接使用独立队列，刷新或多标签页不会互相抢事件。
            while True:
                try:
                    msg = await asyncio.wait_for(event_queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 注释心跳不会触发前端事件，但可防止端口转发/代理断开空闲流。
                    yield ": keep-alive\n\n"
                    continue
                cursor = int(msg.get("cursor") or 0)
                if cursor and cursor <= delivered_cursor:
                    continue
                yield _sse(
                    msg["event"],
                    msg["data"],
                    event_id=cursor or None,
                )
                delivered_cursor = max(delivered_cursor, cursor)
                if msg["event"] in (
                    "done", "error", "cancelled", "rerun_done", "rerun_cancelled",
                ):
                    break
        finally:
            task.unsubscribe(event_queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/history")
def api_history(
    limit: int = 50,
    page: int | None = None,
    page_size: int | None = None,
):
    prune_task_cache()
    if page_size is not None:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(int(page_size), 100))
        rows, history_total = list_snapshots_page(safe_page, safe_size)
        response_page_size = safe_size
    else:
        rows = list_snapshots(limit=limit)
        history_total = len(rows)
        response_page_size = len(rows)
    for row in rows:
        live = get_live_task(str(row.get("task_id") or ""))
        if live is None:
            continue
        row.update({
            "status": live.status,
            "total": active_total(live.items),
            "done": max(
                live.done_total,
                active_result_count(live.items, live.results),
            ),
            "started_at": live.started_at,
            "finished_at": live.finished_at,
            "duration_s": live.elapsed_s(),
            "error": live.error,
            "active_rerun": live.active_rerun,
            "rerun_count": len(live.rerun_history),
            "active_append": live.active_append,
            "append_count": len(live.append_history),
            "excluded_count": len(live.items) - active_total(live.items),
            "dataset_batch_count": len(live.dataset_batches),
        })
    return {
        "items": rows,
        "total": history_total,
        "page": max(1, int(page or 1)),
        "page_size": response_page_size,
    }


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: str, compact: bool = False):
    task = get_live_task(task_id)
    if task is None:
        # 查看旧历史只临时反序列化，不把完整任务永久放入全局缓存。
        task = get_task(task_id, cache=False)
    if task:
        # 先读游标再构建快照：两者之间产生的事件最多重复，
        # 不会因游标超前而丢失。
        event_cursor = task.event_cursor
        payload = snapshot_payload(task_to_snapshot(task), compact=compact)
        payload["event_cursor"] = event_cursor
        return payload
    data = load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    return snapshot_payload(data, compact=compact)


def _operation_history_comparison_payload(
    request: OperationHistoryComparisonReq,
    *,
    include_union_rows: bool = False,
) -> dict:
    task_ids = [str(task_id).strip() for task_id in request.task_ids]
    if not 2 <= len(task_ids) <= 5:
        raise HTTPException(422, "请选择 2～5 个已完成的任务类历史批次")
    if not all(task_ids) or len(set(task_ids)) != len(task_ids):
        raise HTTPException(422, "历史批次不能为空或重复选择")
    if request.baseline_task_id not in task_ids:
        raise HTTPException(422, "对照组必须包含在已选历史批次中")

    batches = []
    for task_id in task_ids:
        live = get_live_task(task_id)
        data = task_to_snapshot(live) if live else load_snapshot(task_id)
        if not data:
            raise HTTPException(404, f"历史任务不存在：{task_id}")
        if data.get("status") != "done":
            raise HTTPException(409, f"只能对比已完成任务：{task_id}")
        try:
            batches.append(operation_comparison_batch(data))
        except ValueError as exc:
            raise HTTPException(409, f"{task_id}：{exc}") from exc
    try:
        return compare_operation_batches(
            batches,
            baseline_task_id=request.baseline_task_id,
            include_union_rows=include_union_rows,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/operation/history-comparison")
def api_operation_history_comparison(request: OperationHistoryComparisonReq):
    """生成历史任务类批次的确定性配对对比 JSON。"""
    return _operation_history_comparison_payload(request)


@app.post("/api/operation/history-comparison/export")
def api_operation_history_comparison_export(
    request: OperationHistoryComparisonReq,
):
    """导出历史任务类批次对比 XLSX。"""
    payload = _operation_history_comparison_payload(
        request,
        include_union_rows=True,
    )
    content = build_operation_comparison_xlsx(payload)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    baseline_name = _download_stem(
        str(payload.get("baseline_name") or "operation"),
        "operation",
    )
    utf8_name = f"{baseline_name}_history_compare_{timestamp}.xlsx"
    ascii_name = f"operation_history_compare_{timestamp}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(utf8_name, safe='')}"
            ),
        },
    )


def _operation_comparison_analysis_payload(
    request: OperationComparisonAnalyzeReq,
    *,
    include_union_rows: bool = False,
    include_report: bool = False,
) -> dict:
    if not 2 <= len(request.sources) <= 5:
        raise HTTPException(422, "请选择 2～5 个任务类评估结果集")
    source_ids = [source.source_id.strip() for source in request.sources]
    if not all(source_ids) or len(set(source_ids)) != len(source_ids):
        raise HTTPException(422, "结果集 source_id 不能为空且不能重复")
    if request.control_source_id not in source_ids:
        raise HTTPException(422, "必须从已选结果集中指定一个对照组")

    batches = []
    for source in request.sources:
        if source.source_type == "history":
            task_id = source.task_id.strip() or source.source_id.strip()
            live = get_live_task(task_id)
            data = task_to_snapshot(live) if live else load_snapshot(task_id)
            if not data:
                raise HTTPException(404, f"历史任务不存在：{task_id}")
            if data.get("status") != "done":
                raise HTTPException(409, f"只能使用已完成任务：{task_id}")
            try:
                batch = operation_comparison_batch(data)
            except ValueError as exc:
                raise HTTPException(409, f"{task_id}：{exc}") from exc
            batch["task_id"] = source.source_id.strip()
        else:
            try:
                batch = validate_uploaded_comparison_source(
                    source.model_dump(mode="python")
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if source.group_name.strip():
            batch["dataset_name"] = source.group_name.strip()
        batches.append(batch)
    try:
        payload = compare_operation_batches(
            batches,
            baseline_task_id=request.control_source_id,
            include_union_rows=include_union_rows,
        )
        if include_report:
            payload["report"] = build_comparison_report(batches, payload)
        return payload
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/operation/comparison/analyze")
def api_operation_comparison_analyze(request: OperationComparisonAnalyzeReq):
    """对历史任务和上传结果集进行混合对比分析。"""
    return _operation_comparison_analysis_payload(request, include_report=True)


@app.post("/api/operation/comparison/export")
def api_operation_comparison_export(
    request: OperationComparisonAnalyzeReq,
    format: Literal["xlsx", "html"] = "xlsx",
):
    """导出独立任务类对比分析报告。"""
    payload = _operation_comparison_analysis_payload(
        request,
        include_union_rows=format == "xlsx",
        include_report=format == "html",
    )
    content = (
        build_operation_report_html(payload["report"])
        if format == "html" else build_operation_comparison_xlsx(payload)
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    control_name = _download_stem(
        str(payload.get("baseline_name") or "operation"),
        "operation",
    )
    utf8_name = f"{control_name}_comparison_{timestamp}.{format}"
    ascii_name = f"operation_comparison_{timestamp}.{format}"
    return Response(
        content,
        media_type=(
            "text/html" if format == "html"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(utf8_name, safe='')}"
            ),
        },
    )


@app.delete("/api/history/{task_id}")
def api_history_delete(task_id: str):
    live = get_live_task(task_id)
    if live is not None and live.status in {"pending", "running", "rerunning"}:
        raise HTTPException(409, "运行中的任务请先中断，再删除历史记录")
    if not delete_snapshot(task_id):
        raise HTTPException(404, "task not found")
    remove_task(task_id)
    return {"ok": True}


@app.get("/api/eval/{task_id}/dataset")
def api_dataset_maintenance(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.mode != "operation":
        raise HTTPException(422, "目前仅任务类（录屏）支持数据集维护")
    if (task.options or {}).get("operation_layout") == "multi_group":
        raise HTTPException(409, "任务类多组评估暂不支持数据集维护")
    had_batches = bool(task.dataset_batches)
    _ensure_dataset_tracking(task)
    if not had_batches and not save_task(task):
        raise HTTPException(500, "旧任务数据集版本初始化失败")
    return _dataset_maintenance_payload(task)


def _set_dataset_item_status(
    task_id: str,
    req: DatasetItemsActionReq,
    *,
    status: str,
) -> dict:
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    _assert_dataset_maintenance_available(task)
    _ensure_dataset_tracking(task)
    indices = list(dict.fromkeys(req.item_indices))
    if not indices:
        raise HTTPException(400, "item_indices 为空")
    invalid = [index for index in indices if index < 0 or index >= len(task.items)]
    if invalid:
        raise HTTPException(422, f"无效的数据集索引：{invalid[:10]}")
    if status == DATASET_ACTIVE:
        rolled_back_batch_ids = {
            str(batch.get("batch_id") or "")
            for batch in task.dataset_batches
            if batch.get("status") == "rolled_back"
        }
        blocked = [
            index for index in indices
            if str(task.items[index].get("dataset_source_batch_id") or "")
            in rolled_back_batch_ids
        ]
        if blocked:
            raise HTTPException(
                409,
                "来自已回滚批次的题目不能直接恢复，请重新追加修正后的数据",
            )

    previous_statuses = {
        index: task.items[index].get("dataset_status") or DATASET_ACTIVE
        for index in indices
    }
    previous_log_size = len(task.dataset_change_log)
    previous_summary = task.summary
    previous_done_total = task.done_total
    changed_indices: list[int] = []
    action = "restore" if status == DATASET_ACTIVE else "exclude"
    for index in indices:
        if previous_statuses[index] == status:
            continue
        task.items[index]["dataset_status"] = status
        changed_indices.append(index)
        _dataset_change(
            task,
            action=action,
            item_index=index,
            reason=req.reason,
            details={
                "before_status": previous_statuses[index],
                "after_status": status,
            },
        )
    if not changed_indices:
        return _dataset_maintenance_payload(task)

    refresh_task_summary(task, cfg())
    if not save_task(task):
        for index, previous in previous_statuses.items():
            task.items[index]["dataset_status"] = previous
        del task.dataset_change_log[previous_log_size:]
        task.summary = previous_summary
        task.done_total = previous_done_total
        raise HTTPException(500, "数据集修订保存失败，已撤销本次操作")
    task.publish_nowait("dataset_changed", {
        "action": action,
        "item_indices": changed_indices,
        "active_count": active_total(task.items),
        "excluded_count": len(task.items) - active_total(task.items),
    })
    return _dataset_maintenance_payload(task)


@app.post("/api/eval/{task_id}/dataset/items/exclude")
def api_dataset_items_exclude(task_id: str, req: DatasetItemsActionReq):
    return _set_dataset_item_status(task_id, req, status=DATASET_EXCLUDED)


@app.post("/api/eval/{task_id}/dataset/items/restore")
def api_dataset_items_restore(task_id: str, req: DatasetItemsActionReq):
    return _set_dataset_item_status(task_id, req, status=DATASET_ACTIVE)


@app.post("/api/eval/{task_id}/dataset/batches/{batch_id}/rollback")
def api_dataset_batch_rollback(
    task_id: str,
    batch_id: str,
    req: DatasetBatchRollbackReq,
):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    _assert_dataset_maintenance_available(task)
    _ensure_dataset_tracking(task)
    batch = next(
        (row for row in task.dataset_batches if row.get("batch_id") == batch_id),
        None,
    )
    if batch is None:
        raise HTTPException(404, "追加批次不存在")
    if batch.get("kind") != "append":
        raise HTTPException(409, "初始数据集不能整批回滚")
    if batch.get("status") != "active":
        raise HTTPException(409, "该追加批次已经回滚")
    active_append_batches = [
        row for row in task.dataset_batches
        if row.get("kind") == "append" and row.get("status") == "active"
    ]
    if not active_append_batches or active_append_batches[-1] is not batch:
        raise HTTPException(409, "为避免覆盖后续修订，请从最后一个有效追加批次开始回滚")

    inserted_indices = [int(value) for value in batch.get("inserted_item_indices") or []]
    rollback_data = load_dataset_artifact(batch.get("rollback_path") or "")
    replacements = (
        rollback_data.get("replacements") or []
        if isinstance(rollback_data, dict) else []
    )
    if (inserted_indices or batch.get("replaced_item_indices")) and not isinstance(
        rollback_data, dict,
    ):
        raise HTTPException(409, "该批次缺少回滚资料，未修改当前数据集")

    previous_state = {
        "items": deepcopy(task.items),
        "results": deepcopy(task.results),
        "item_progress": deepcopy(task.item_progress),
        "progress_events": deepcopy(task.progress_events),
        "dataset_batches": deepcopy(task.dataset_batches),
        "dataset_change_log": deepcopy(task.dataset_change_log),
        "summary": deepcopy(task.summary),
        "done_total": task.done_total,
        "status": task.status,
        "error": task.error,
    }
    rolled_back_at = time.time()
    for index in inserted_indices:
        if 0 <= index < len(task.items):
            task.items[index]["dataset_status"] = DATASET_EXCLUDED
    for backup in replacements:
        index = int(backup.get("item_index", -1))
        before_item = backup.get("before_item")
        if not (0 <= index < len(task.items) and isinstance(before_item, dict)):
            continue
        task.items[index] = before_item
        task.results = [
            row for row in task.results if _web_result_index(row) != index
        ]
        before_result = backup.get("before_result")
        if isinstance(before_result, dict):
            task.results.append(before_result)
        before_progress = backup.get("before_progress")
        if isinstance(before_progress, dict):
            task.item_progress[str(index)] = before_progress
        else:
            task.item_progress.pop(str(index), None)
            task.item_progress.pop(index, None)
        before_events = backup.get("before_progress_events")
        if isinstance(before_events, list) and before_events:
            task.progress_events[str(index)] = before_events
        else:
            task.progress_events.pop(str(index), None)
            task.progress_events.pop(index, None)
    task.results.sort(key=lambda row: (_web_result_index(row) is None, _web_result_index(row) or 0))
    batch["status"] = "rolled_back"
    batch["rolled_back_at"] = rolled_back_at
    batch["rollback_reason"] = req.reason.strip()
    for attempt in task.append_history:
        if (
            attempt.get("batch_id") == batch_id
            or attempt.get("append_id") == batch.get("append_id")
        ):
            attempt["dataset_batch_status"] = "rolled_back"
            attempt["dataset_rolled_back_at"] = rolled_back_at
    _dataset_change(
        task,
        action="rollback_batch",
        batch_id=batch_id,
        reason=req.reason,
        details={
            "inserted_count": len(inserted_indices),
            "replaced_count": len(replacements),
        },
    )
    refresh_task_summary(task, cfg())
    base_status = str(batch.get("base_status") or task.status)
    if base_status in {"done", "error", "cancelled"}:
        task.status = base_status
        task.error = batch.get("base_error")
    if not save_task(task):
        task.items = previous_state["items"]
        task.results = previous_state["results"]
        task.item_progress = previous_state["item_progress"]
        task.progress_events = previous_state["progress_events"]
        task.dataset_batches = previous_state["dataset_batches"]
        task.dataset_change_log = previous_state["dataset_change_log"]
        task.summary = previous_state["summary"]
        task.done_total = previous_state["done_total"]
        task.status = previous_state["status"]
        task.error = previous_state["error"]
        raise HTTPException(500, "追加批次回滚保存失败，已撤销本次操作")
    task.publish_nowait("dataset_changed", {
        "action": "rollback_batch",
        "batch_id": batch_id,
        "active_count": active_total(task.items),
        "excluded_count": len(task.items) - active_total(task.items),
    })
    return _dataset_maintenance_payload(task)


@app.get("/api/eval/{task_id}/dataset/batches/{batch_id}/export")
def api_dataset_batch_export(task_id: str, batch_id: str):
    task = get_task(task_id, cache=False)
    if task is None:
        raise HTTPException(404, "task not found")
    batch = next(
        (row for row in task.dataset_batches if row.get("batch_id") == batch_id),
        None,
    )
    if batch is None:
        raise HTTPException(404, "数据集批次不存在")
    path = dataset_artifact_path(batch.get("snapshot_path") or "")
    if path is None:
        raise HTTPException(404, "该批次没有可下载的数据快照")
    source_stem = Path(str(batch.get("source_dataset_name") or "dataset")).stem
    filename = f"{int(batch.get('batch_no') or 0):04d}_{source_stem}.jsonl"
    return FileResponse(path, media_type="application/x-ndjson", filename=filename)


@app.post("/api/eval/{task_id}/cancel")
async def api_eval_cancel(task_id: str):
    task = get_live_task(task_id)
    if task is None:
        raise HTTPException(404, "当前服务中没有这个运行任务")
    if task.active_append:
        execution = task.execution
        if execution is not None and not execution.done():
            execution.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # 极短窗口内取消时，run_append 可能尚未进入 finally；这里兜底落盘。
        if task.active_append:
            attempt = dict(task.active_append)
            finished_at = datetime.now().timestamp()
            attempt.update({
                "status": "cancelled",
                "error": "用户手动中断追加评估",
                "finished_at": finished_at,
            })
            started_at = float(attempt.get("started_at") or finished_at)
            attempt["duration_s"] = round(max(0.0, finished_at - started_at), 3)
            base_duration_s = float(
                attempt.pop("base_duration_s", task.duration_s or 0.0)
            )
            task.duration_s = round(base_duration_s + attempt["duration_s"], 3)
            task.finished_at = finished_at
            task.append_history.append(attempt)
            task.active_append = None
            task.status = "cancelled"
            task.error = attempt["error"]
            for index in attempt.get("item_indices") or []:
                previous = task.item_progress.get(str(index)) or {}
                if previous.get("status") in {"done", "error", "cancelled"}:
                    continue
                task.item_progress[str(index)] = {
                    **previous,
                    "item_index": index,
                    "item_id": task.items[index].get("id") or f"q{index}",
                    "status": "cancelled",
                    "percent": previous.get("percent", 0),
                    "message": "追加评估已中断",
                    "finished_at": finished_at,
                }
            await task.publish("cancelled", {
                "message": task.error,
                "duration_s": task.duration_s,
                "append": attempt,
            })
            save_task(task)
        return {"ok": True, "task_id": task.id, "status": task.status}
    if task.status == "rerunning":
        execution = task.execution
        if execution is not None and not execution.done():
            execution.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # create_task 若在首次调度前即被取消，run_rerun 的 finally 不会执行；
        # 这里兜底恢复父任务，避免历史永久卡在“重跑中”。
        if task.status == "rerunning":
            attempt = dict(task.active_rerun or {})
            attempt["status"] = "cancelled"
            attempt["error"] = "用户手动中断重跑"
            attempt["finished_at"] = datetime.now().timestamp()
            if attempt.get("started_at") is not None:
                attempt["duration_s"] = round(max(
                    0.0,
                    attempt["finished_at"] - float(attempt["started_at"]),
                ), 3)
            task.rerun_history.append(attempt)
            task.active_rerun = None
            task.status = str(attempt.get("base_status") or "done")
            await task.publish("rerun_cancelled", {
                "attempt": attempt,
                "summary": task.summary,
                "status": task.status,
                "progress": task.done_total,
                "total": active_total(task.items),
            })
            save_task(task)
        return {"ok": True, "task_id": task.id, "status": task.status}
    if task.status not in {"pending", "running"}:
        return {"ok": True, "task_id": task.id, "status": task.status}

    reason = "用户手动中断批跑"
    task.status = "cancelled"
    task.error = reason
    task.mark_finished()
    updated_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    for index, item in enumerate(task.items):
        key = str(index)
        previous = task.item_progress.get(key) or {}
        if previous.get("status") in {"done", "error", "cancelled"}:
            continue
        events = task.progress_events.setdefault(key, [])
        payload = {
            **previous,
            "item_index": index,
            "item_id": item.get("id") or f"q{index}",
            "status": "cancelled",
            "message": "任务已手动中断",
            "percent": previous.get("percent", 0),
            "sequence": int(events[-1].get("sequence", 0)) + 1 if events else 1,
            "updated_at": updated_at,
        }
        events.append(payload)
        del events[:-100]
        task.item_progress[key] = payload
        task.publish_nowait("item_progress", payload)

    execution = task.execution
    if execution is not None and not execution.done():
        execution.cancel()
    for item_execution in list(task.item_executions.values()):
        if not item_execution.done():
            item_execution.cancel()
    await task.publish(
        "cancelled",
        {"message": reason, "duration_s": task.duration_s},
    )
    if not save_task(task):
        raise HTTPException(500, "任务已中断，但历史状态保存失败")
    return {"ok": True, "task_id": task.id, "status": task.status}


@app.post("/api/eval/{task_id}/rerun")
async def api_eval_rerun(task_id: str, req: RerunReq):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.status in {"pending", "running", "rerunning"}:
        raise HTTPException(409, "当前任务仍在运行，请等待完成或先中断")
    if task.execution is not None and not task.execution.done():
        raise HTTPException(409, "当前任务已有执行中的操作")

    indices = list(dict.fromkeys(req.item_indices))
    if not indices:
        raise HTTPException(400, "item_indices 为空")
    invalid = [index for index in indices if index < 0 or index >= len(task.items)]
    if invalid:
        raise HTTPException(422, f"无效的数据集索引：{invalid[:10]}")
    excluded = [index for index in indices if not is_item_active(task.items[index])]
    if excluded:
        raise HTTPException(409, f"已排除题目不能重跑：{excluded[:10]}")

    rerun_options = dict(task.options)
    # 新前端会显式提交当前选择；旧客户端未提交时继续沿用原任务配置。
    if "judge_backend" in req.model_fields_set:
        if req.judge_backend:
            rerun_options["judge_backend"] = req.judge_backend
        else:
            rerun_options.pop("judge_backend", None)
    try:
        normalized_rerun_options, runtime_cfg = _normalize_eval_options(
            cfg(),
            rerun_options,
        )
    except KeyError as exc:
        raise HTTPException(422, f"Provider 不存在：{exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    base_status = task.status
    task.status = "rerunning"
    attempt_no = len(task.rerun_history) + 1
    rerun_backend = normalized_rerun_options.get("judge_backend") or {}
    task.active_rerun = {
        "attempt_id": f"rerun-{attempt_no}-{uuid.uuid4().hex[:8]}",
        "attempt_no": attempt_no,
        "item_indices": indices,
        "total": len(indices),
        "done": 0,
        "status": "starting",
        "base_status": base_status,
        "started_at": datetime.now().timestamp(),
        "judge_backend": rerun_backend,
        "items": [],
    }
    save_task(task)
    execution = asyncio.create_task(
        run_rerun(task, runtime_cfg, indices, base_status=base_status),
    )
    task.execution = execution

    def clear_execution(finished: asyncio.Task) -> None:
        if task.execution is finished:
            task.execution = None

    execution.add_done_callback(clear_execution)
    return {
        "ok": True,
        "task_id": task.id,
        "status": task.status,
        "item_indices": indices,
        "judge_backend": rerun_backend,
    }


@app.patch("/api/history/{task_id}/note")
def api_history_note(task_id: str, req: HistoryNoteReq):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    note = req.note.strip()
    if len(note) > 1000:
        raise HTTPException(422, "备注不能超过 1000 个字符")
    task.note = note
    if not save_task(task):
        raise HTTPException(500, "备注保存失败")
    return {"ok": True, "task_id": task.id, "note": task.note}


def _download_stem(value: str, fallback: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_. " else "_"
        for char in value
    ).strip(" ._")
    return safe[:120] or fallback


def _eval_download_names(
    data: dict,
    task_id: str,
    extension: str,
) -> tuple[str, str]:
    """返回评估导出的 ASCII 回退文件名和 UTF-8 完整文件名。"""
    raw_dataset_name = str(data.get("dataset_name") or "").replace("\\", "/")
    dataset_stem = Path(raw_dataset_name).stem if raw_dataset_name else ""
    safe_dataset = _download_stem(dataset_stem, "")[:80]

    timestamp_value = data.get("created_at") or data.get("updated_at")
    try:
        timestamp = datetime.fromtimestamp(float(timestamp_value)).strftime("%Y%m%d_%H%M%S")
    except (TypeError, ValueError, OSError, OverflowError):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    short_task_id = _download_stem(str(task_id), "task")[:8]
    suffix = f"eval_{timestamp}_{short_task_id}.{extension.lstrip('.')}"
    utf8_name = f"{safe_dataset}_{suffix}" if safe_dataset else suffix
    ascii_name = suffix
    return ascii_name, utf8_name


@app.get("/api/eval/{task_id}/statistics")
def api_operation_statistics(task_id: str, download: bool = False):
    """返回任务类单批统计 JSON；下载的 Excel 统计 Sheet 使用同一结构。"""
    task = get_live_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    try:
        payload = operation_statistics_payload(data)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not download:
        return JSONResponse(payload)

    dataset_stem = Path(str(payload.get("dataset_name") or "")).stem
    utf8_name = f"{_download_stem(dataset_stem, 'operation')}_statistics.json"
    ascii_name = f"operation_statistics_{_download_stem(task_id, 'task')[:8]}.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(utf8_name, safe='')}"
            ),
        },
    )


def _single_operation_report(data: dict) -> dict:
    try:
        statistics = operation_statistics_payload(data)
        return build_single_report(
            operation_comparison_batch(data), statistics["statistics"],
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/eval/{task_id}/report")
def api_operation_report(task_id: str):
    """最新结果的紧凑报告数据；不缓存，重跑后重新计算。"""
    task = get_live_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    return JSONResponse(
        _single_operation_report(data),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/eval/{task_id}/export")
def api_export(task_id: str, format: str = "json"):
    task = get_live_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")

    if format == "json":
        return JSONResponse(snapshot_payload(data))

    if format == "html":
        content = build_operation_report_html(_single_operation_report(data))
        ascii_name, utf8_name = _eval_download_names(data, task_id, "html")
        return Response(
            content,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(utf8_name, safe='')}"
                ),
            },
        )

    if format == "jsonl":
        try:
            content = rows_to_jsonl(jsonl_export_rows(data))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        ascii_name, utf8_name = _eval_download_names(data, task_id, "jsonl")
        return StreamingResponse(
            iter([content.encode("utf-8")]),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(utf8_name, safe='')}"
                ),
            },
        )

    if format == "xlsx":
        content = build_xlsx(data, cfg())
        ascii_name, utf8_name = _eval_download_names(data, task_id, "xlsx")
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(utf8_name, safe='')}"
                ),
            },
        )

    if format in {"frames", "frames_zip"}:
        export_dir = RUNS_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / f".{task_id}.{uuid.uuid4().hex}.zip"
        write_frames_zip(data, archive_path)
        raw_name = Path(str(data.get("dataset_name") or f"eval_{task_id}")).stem
        safe_name = "".join(
            char if char.isalnum() or char in "-_. " else "_"
            for char in raw_name
        ).strip(" ._") or f"eval_{task_id}"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{safe_name}_frames.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    if format == "csv":
        sheets = export_rows(data, cfg())
        csv_text = rows_to_csv(sheets.get("逐题结果") or [])
        return StreamingResponse(
            iter([csv_text.encode("utf-8-sig")]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.csv"},
        )

    raise HTTPException(400, f"不支持的导出格式：{format}")


@app.get("/api/eval/{task_id}/items/{item_index}/export")
def api_export_item(
    task_id: str,
    item_index: int,
    format: str,
    image_index: int = 0,
):
    """导出单条结果关联的原视频、原输入图片、关键帧或裁判调用 JSON。"""
    task = get_live_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    items = data.get("items") or []
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(404, "item not found")
    item = items[item_index]
    raw_id = str(item.get("id") or f"q{item_index + 1}")
    stem = _download_stem(
        f"{item_index + 1:03d}_{raw_id}",
        f"{item_index + 1:03d}_item",
    )

    if format in {"query_image", "query-image"}:
        raw_images = list(item.get("query_images") or [])
        if image_index < 0 or image_index >= len(raw_images):
            raise HTTPException(404, "该条结果没有对应的原输入图片")
        try:
            image_path = resolve_operation_query_image_path(
                str(raw_images[image_index]),
                base_dir=BASE_DIR,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        return FileResponse(
            image_path,
            media_type=media_types.get(image_path.suffix.lower()),
        )

    if format == "video":
        raw_path = str(item.get("video_path") or "").strip()
        if not raw_path:
            media = item.get("media") or []
            raw_path = str(media[0]).strip() if media else ""
        if not raw_path:
            raise HTTPException(404, "该条结果没有原始视频路径")
        try:
            video_path = _resolve_operation_video_path(raw_path)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(
            video_path,
            filename=f"{stem}{video_path.suffix.lower()}",
        )

    if format in {"frames", "frames_zip"}:
        export_dir = RUNS_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / (
            f".{task_id}.{item_index}.{uuid.uuid4().hex}.zip"
        )
        write_frames_zip(data, archive_path, item_indexes={item_index})
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{stem}_frames.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    if format in {"judge", "judge_calls"}:
        payload = load_item_judge_calls(data, item_index)
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    f"{quote(f'{stem}_judge_calls.json')}"
                ),
            },
        )

    raise HTTPException(
        400,
        "format 必须是 query_image、video、frames_zip 或 judge_calls",
    )


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace(STATIC_VERSION_TOKEN, _static_asset_version())
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


app.mount(
    "/report-assets",
    VersionedStaticFiles(directory=str(OPERATION_REPORT_ASSETS)),
    name="report-assets",
)

app.mount(
    "/static",
    VersionedStaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8502)
