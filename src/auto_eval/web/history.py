"""Web 评测历史持久化与完整导出。

这里刻意不用数据库：评测台是本地/轻量服务，JSON 快照足够支撑历史加载；
XLSX 直接生成 OOXML，避免给项目额外引入 openpyxl / xlsxwriter 依赖。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
import zipfile
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from ..analysis.operation_statistics import summarize_operation_results
from ..judges.operation_fields import map_legacy_operation_result
from ..judges.trace_storage import (
    configured_legacy_trace_path,
    configured_task_trace_path,
    configured_write_trace_path,
    trace_path_reference,
)
from ..paths import PROJECT_ROOT, RUNS_DIR


HISTORY_DIR = RUNS_DIR / "web_history"
logger = logging.getLogger(__name__)


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "_", value)


def make_session_name(created_at: float, mode: str, task_id: str) -> str:
    """生成可按文件名排序、同时能关联任务的稳定会话名。"""
    dt = datetime.fromtimestamp(created_at).astimezone()
    return f"{dt:%Y%m%d_%H%M%S}_{_safe_name(mode)}_{_safe_name(task_id)}"


def _find_task_path(task_id: str) -> Path:
    """优先查旧文件名，再查带时间前缀的新文件名。"""
    safe = _safe_name(task_id)
    legacy = HISTORY_DIR / f"{safe}.json"
    if legacy.exists():
        return legacy
    matches = sorted(HISTORY_DIR.glob(f"*_{safe}.json"))
    return matches[-1] if matches else legacy


def _task_path(task_id: str, session_name: str = "") -> Path:
    if session_name:
        return HISTORY_DIR / f"{_safe_name(session_name)}.json"
    return _find_task_path(task_id)


def task_to_snapshot(task) -> dict:
    judge_trace_path = configured_write_trace_path(task.id, task.session_name)
    judge_trace_reference = getattr(task, "judge_trace_path", "") or (
        trace_path_reference(judge_trace_path) if judge_trace_path else ""
    )
    return {
        "task_id": task.id,
        "session_name": task.session_name,
        "mode": task.mode,
        "dataset_name": getattr(task, "dataset_name", ""),
        "note": getattr(task, "note", ""),
        "items": task.items,
        "options": task.options,
        "status": task.status,
        "results": task.results,
        "item_progress": task.item_progress,
        "progress_events": task.progress_events,
        "summary": task.summary,
        "created_at": task.created_at,
        "started_at": getattr(task, "started_at", None),
        "finished_at": getattr(task, "finished_at", None),
        "duration_s": getattr(task, "duration_s", None),
        "updated_at": time.time(),
        "done_total": task.done_total,
        "event_cursor": getattr(task, "event_cursor", 0),
        "error": task.error,
        "active_rerun": getattr(task, "active_rerun", None),
        "rerun_history": getattr(task, "rerun_history", []),
        "judge_trace_path": judge_trace_reference,
    }


def save_task(task, *, max_attempts: int = 3) -> bool:
    """Best-effort atomic snapshot save.

    Snapshot persistence must never terminate an evaluation.  A unique
    temporary file avoids concurrent writers sharing the same ``.tmp`` path;
    short retries cover transient Windows file locks.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _task_path(task.id, getattr(task, "session_name", ""))
    content = json.dumps(task_to_snapshot(task), ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt + 1 < attempts:
                time.sleep(0.02 * (attempt + 1))
    logger.error(
        "保存任务快照失败: task_id=%s attempts=%s error=%s",
        getattr(task, "id", "-"),
        attempts,
        last_error,
    )
    return False


def load_snapshot(task_id: str) -> dict | None:
    path = _find_task_path(task_id)
    if not path.exists():
        return None
    try:
        return _with_operation_compat(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _stored_timing(data: dict) -> dict[str, float | None]:
    """读取已持久化的批跑时间；不使用 updated_at 猜测结束时间。"""
    started_at = data.get("started_at")
    finished_at = data.get("finished_at")
    duration_s = data.get("duration_s")
    try:
        started_at = float(started_at) if started_at is not None else None
    except (TypeError, ValueError):
        started_at = None
    try:
        finished_at = float(finished_at) if finished_at is not None else None
    except (TypeError, ValueError):
        finished_at = None
    try:
        duration_s = float(duration_s) if duration_s is not None else None
    except (TypeError, ValueError):
        duration_s = None
    if duration_s is None and started_at is not None and finished_at is not None:
        duration_s = round(max(0.0, finished_at - started_at), 3)
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": duration_s,
    }


def delete_snapshot(task_id: str) -> bool:
    """删除某次评测的快照文件。返回是否删除成功（文件存在且已删除）。"""
    path = _find_task_path(task_id)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        return False


def list_snapshots(limit: int = 50) -> list[dict]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in HISTORY_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = data.get("status")
        error = data.get("error")
        if status == "rerunning":
            status = str((data.get("active_rerun") or {}).get("base_status") or "done")
        elif status in {"pending", "running"}:
            status = "error"
            error = error or "服务中断，已保留中断前完成的评估结果"
        task_id = data.get("task_id") or path.stem
        created_at = data.get("created_at")
        session_name = data.get("session_name") or (
            path.stem
            if path.stem != _safe_name(str(task_id))
            else make_session_name(float(created_at or 0), data.get("mode") or "unknown", str(task_id))
        )
        rows.append({
            "task_id": task_id,
            "session_name": session_name,
            "dataset_name": data.get("dataset_name") or "",
            "note": data.get("note") or "",
            "mode": data.get("mode"),
            "operation_layout": (data.get("options") or {}).get("operation_layout") or "single",
            "status": status,
            "total": len(data.get("items") or []),
            "done": len([r for r in (data.get("results") or []) if "error" not in r]),
            "created_at": created_at,
            "updated_at": data.get("updated_at") or data.get("created_at"),
            **_stored_timing(data),
            "error": error,
            "active_rerun": data.get("active_rerun"),
            "rerun_count": len(data.get("rerun_history") or []),
            **_judge_backend_summary(data),
            "preview": _preview(data),
        })
    rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return rows[:limit] if limit > 0 else rows


def list_snapshots_page(page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    """按会话文件名分页读取历史，避免每次刷新解析全部大快照。

    新历史文件名以 ``YYYYMMDD_HHMMSS`` 开头，可直接按名称
    倒序排列；旧文件放在其后并按修改时间排序。
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    safe_page = max(1, int(page or 1))
    safe_size = max(1, min(int(page_size or 10), 100))
    paths = list(HISTORY_DIR.glob("*.json"))

    def sort_key(path: Path) -> tuple[int, str, float]:
        dated = bool(re.match(r"^\d{8}_\d{6}_", path.name))
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (1 if dated else 0, path.name if dated else "", modified)

    paths.sort(key=sort_key, reverse=True)
    total = len(paths)
    start = (safe_page - 1) * safe_size
    selected_paths = paths[start:start + safe_size]
    rows: list[dict] = []
    for path in selected_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = data.get("status")
        error = data.get("error")
        if status == "rerunning":
            status = str((data.get("active_rerun") or {}).get("base_status") or "done")
        elif status in {"pending", "running"}:
            status = "error"
            error = error or "服务中断，已保留中断前完成的评估结果"
        task_id = data.get("task_id") or path.stem
        created_at = data.get("created_at")
        session_name = data.get("session_name") or (
            path.stem
            if path.stem != _safe_name(str(task_id))
            else make_session_name(
                float(created_at or 0),
                data.get("mode") or "unknown",
                str(task_id),
            )
        )
        rows.append({
            "task_id": task_id,
            "session_name": session_name,
            "dataset_name": data.get("dataset_name") or "",
            "note": data.get("note") or "",
            "mode": data.get("mode"),
            "operation_layout": (data.get("options") or {}).get("operation_layout") or "single",
            "status": status,
            "total": len(data.get("items") or []),
            "done": len([
                result for result in (data.get("results") or [])
                if "error" not in result
            ]),
            "created_at": created_at,
            "updated_at": data.get("updated_at") or created_at,
            **_stored_timing(data),
            "error": error,
            "active_rerun": data.get("active_rerun"),
            "rerun_count": len(data.get("rerun_history") or []),
            **_judge_backend_summary(data),
            "preview": _preview(data),
        })
    return rows, total

def _preview(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return ""
    q = str(items[0].get("query") or "")
    return q[:80] + ("…" if len(q) > 80 else "")


def _judge_backend_summary(snapshot: dict) -> dict[str, str]:
    backend = (snapshot.get("options") or {}).get("judge_backend") or {}
    return {
        "judge_provider": str(backend.get("provider_name") or "角色默认配置"),
        "judge_provider_id": str(backend.get("provider_id") or ""),
        "judge_model": str(backend.get("model") or ""),
        "judge_provider_revision": str(backend.get("provider_revision") or ""),
    }


def snapshot_payload(data: dict, *, compact: bool = False) -> dict:
    data = _with_operation_compat(data)
    items = data.get("items") or []
    results = data.get("results") or []
    progress_events = data.get("progress_events") or {}
    if compact:
        item_fields = {
            "id", "query", "question", "context", "category", "source_line",
            "case_id", "evaluation_strategy", "alignment_status",
            "alignment_warnings", "group_variants", "image_input",
        }
        items = [
            {key: value for key, value in item.items() if key in item_fields}
            for item in items
        ]
        # 历史恢复页只需要每题最近两条进度摘要；完整日志仍保留在快照中。
        progress_events = {
            str(index): list(events)[-2:]
            for index, events in progress_events.items()
        }
    try:
        saved_done_total = int(data.get("done_total") or 0)
    except (TypeError, ValueError):
        saved_done_total = 0
    done_total = max(saved_done_total, len(results))
    return {
        "task_id": data.get("task_id"),
        "session_name": data.get("session_name"),
        "dataset_name": data.get("dataset_name") or "",
        "note": data.get("note") or "",
        "mode": data.get("mode"),
        "items": items,
        "options": data.get("options") or {},
        "status": data.get("status"),
        "results": results,
        "item_progress": data.get("item_progress") or {},
        "progress_events": progress_events,
        "summary": data.get("summary") or {},
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        **_stored_timing(data),
        # 总进度作为历史详情的显式契约，供新标签页直接恢复。
        "done_total": done_total,
        "total": len(items),
        "event_cursor": int(data.get("event_cursor") or 0),
        "error": data.get("error"),
        "active_rerun": data.get("active_rerun"),
        "rerun_history": data.get("rerun_history") or [],
    }


_JSONL_RESERVED_SOURCE_FIELDS = {
    "dataset_index",
    "source_line",
    "frames_dir",
    "evaluation",
    "eval_run",
}

_JSONL_DUPLICATE_RESULT_FIELDS = {
    "index",
    "item_id",
    "query",
    "context",
    "answer",
    "has_video",
    "category",
    "category_display",
    "category_source",
    "评估状态",
}

_JSONL_INTERNAL_RESULT_FIELDS = {
    "tool_trace",
    "used_search",
    "truncated",
    "low_agreement",
    "arbitrated",
    "arbitrator_confidence",
}

_JSONL_EVALUATION_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("task_type", None),
    ("execution_routes", []),
    ("route_evidence", []),
    ("route_rationale", ""),
    ("route_status", None),
    ("correctness", None),
    ("issue_types", []),
    ("is_low_level", None),
    ("total", None),
    ("rubric", {}),
    ("rubric_reasons", {}),
    ("na_dimensions", []),
    ("rationale", ""),
    ("latency_s", None),
    ("video_prepare_warnings", []),
    ("rerun_count", 0),
    ("last_rerun_at", None),
    ("last_rerun_attempt_id", None),
)

_LEGACY_SEQUENCE_RE = re.compile(
    r"(?:^|_)((?:simple|complex)_\d+(?:_v\d+)?)$",
    re.IGNORECASE,
)


def _jsonl_eval_run(snapshot: dict) -> dict:
    options = snapshot.get("options") or {}
    return {
        "task_id": snapshot.get("task_id"),
        "session_name": snapshot.get("session_name") or "",
        "dataset_name": snapshot.get("dataset_name") or "",
        "mode": snapshot.get("mode") or "",
        "status": snapshot.get("status") or "",
        "created_at": _format_ts(snapshot.get("created_at")),
        "updated_at": _format_ts(snapshot.get("updated_at")),
        "judges": list(options.get("judges") or []),
        "visual_judge": options.get("visual_judge") or "",
        "model": options.get("model") or "",
        **_judge_backend_summary(snapshot),
        "concurrency": options.get("concurrency"),
        "eval_timeout_s": options.get("eval_timeout_s"),
        "rerun_count": len(snapshot.get("rerun_history") or []),
    }


def _jsonl_evaluation(
    result: dict | None,
    progress: dict,
    task_status: str,
) -> dict:
    if result is not None:
        status = "failed" if result.get("error") else "completed"
    else:
        progress_status = str(progress.get("status") or "")
        if progress_status == "error":
            status = "failed"
        elif progress_status == "cancelled" or task_status == "cancelled":
            status = "cancelled"
        elif progress_status == "running":
            status = "running"
        else:
            status = "pending"

    error = (result or {}).get("error") or progress.get("error")
    if not error and status == "failed":
        error = progress.get("message") or "评估失败"
    evaluation: dict[str, Any] = {
        "status": status,
        "error": error or None,
    }
    for key, default in _JSONL_EVALUATION_DEFAULTS:
        value = (result or {}).get(key, default)
        # 防止可变默认值被调用方意外共享修改。
        evaluation[key] = dict(value) if isinstance(value, dict) else (
            list(value) if isinstance(value, list) else value
        )

    excluded = _JSONL_DUPLICATE_RESULT_FIELDS | _JSONL_INTERNAL_RESULT_FIELDS
    for key, value in (result or {}).items():
        if key not in excluded and key not in evaluation and key != "error":
            evaluation[key] = value
    return evaluation


def _jsonl_sequence(source: dict, item: dict, item_id: str) -> str:
    """读取原始序号；兼容旧转换脚本只将序号拼入 id 的历史数据。"""
    for value in (source.get("序号"), item.get("序号")):
        if value is not None and str(value).strip():
            return str(value).strip()
    match = _LEGACY_SEQUENCE_RE.search(item_id)
    return match.group(1) if match else ""


def jsonl_export_rows(snapshot: dict) -> list[dict]:
    """生成一题一行的完整 JSONL 数据。

    原始输入字段保留在第一层；系统评估结果和批跑元信息分别放入
    ``evaluation`` 与 ``eval_run``，避免覆盖源数据自带的 status/error。
    """
    snapshot = _with_operation_compat(snapshot)
    if (
        snapshot.get("mode") == "operation"
        and (snapshot.get("options") or {}).get("operation_layout") == "multi_group"
    ):
        return _operation_multi_jsonl_rows(snapshot)
    items = snapshot.get("items") or []
    results = _results_with_identity(snapshot)
    by_index: dict[int, dict] = {}
    by_item_id: dict[str, dict] = {}
    for result in results:
        try:
            index = int(result.get("index"))
        except (TypeError, ValueError):
            index = -1
        if index >= 0:
            by_index[index] = result
        item_id = str(result.get("item_id") or "").strip()
        if item_id:
            by_item_id[item_id] = result

    eval_run = _jsonl_eval_run(snapshot)
    item_progress = snapshot.get("item_progress") or {}
    rows: list[dict] = []
    for index, item in enumerate(items):
        source = _source_data_for_item(item)
        conflicts = sorted(_JSONL_RESERVED_SOURCE_FIELDS.intersection(source))
        if conflicts:
            item_id = item.get("id") or f"q{index}"
            raise ValueError(
                f"数据 {item_id} 包含 JSONL 导出保留字段：{', '.join(conflicts)}"
            )

        row = dict(source)
        item_id = str(item.get("id") or row.get("id") or f"q{index}")
        row.setdefault("id", item_id)
        sequence = _jsonl_sequence(source, item, item_id)
        if sequence:
            row.setdefault("序号", sequence)
        row.setdefault("query", item.get("query") or item.get("question") or "")
        if item.get("context") is not None:
            row.setdefault("context", item.get("context"))
        if item.get("answer") is not None:
            row.setdefault("answer", item.get("answer"))
        if item.get("video_path") is not None:
            row.setdefault("video_path", _project_relative_path(item.get("video_path")))

        frames = [Path(str(path)) for path in (item.get("frames") or [])]
        frames_dir = _project_relative_path(frames[0].parent) if frames else ""
        result = by_index.get(index) or by_item_id.get(item_id)
        progress = item_progress.get(str(index)) or item_progress.get(index) or {}
        row.update({
            "dataset_index": index + 1,
            "source_line": item.get("source_line") or index + 1,
            "frames_dir": frames_dir,
            "evaluation": _jsonl_evaluation(
                result,
                progress,
                str(snapshot.get("status") or ""),
            ),
            "eval_run": dict(eval_run),
        })
        rows.append(row)
    return rows


def _operation_multi_jsonl_rows(snapshot: dict) -> list[dict]:
    """多组任务类按 case × 实验组导出，保留每组原始 JSONL 字段。"""
    results_by_index = {
        int(row.get("index")): row
        for row in (snapshot.get("results") or [])
        if str(row.get("index", "")).isdigit()
    }
    eval_run = _jsonl_eval_run(snapshot)
    rows: list[dict] = []
    for case_index, case in enumerate(snapshot.get("items") or []):
        case_result = results_by_index.get(case_index) or {}
        result_by_group = {
            str(row.get("group_id") or ""): row
            for row in (case_result.get("group_results") or [])
        }
        for variant in case.get("group_variants") or []:
            group_id = str(variant.get("group_id") or "")
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            source = _source_data_for_item(item) if item else {}
            row = dict(source)
            item_id = str(item.get("id") or row.get("id") or "")
            if item_id:
                row.setdefault("id", item_id)
            row.setdefault("case_id", case.get("case_id") or case.get("id") or "")
            row.setdefault("query", item.get("query") or case.get("query") or "")
            if item.get("context") is not None:
                row.setdefault("context", item.get("context"))
            if item.get("video_path") is not None:
                row.setdefault("video_path", _project_relative_path(item.get("video_path")))
            frames = [Path(str(path)) for path in (item.get("frames") or [])]
            group_result = result_by_group.get(group_id)
            evaluation = _jsonl_evaluation(
                group_result,
                {},
                str(snapshot.get("status") or ""),
            )
            evaluation["status"] = (
                (group_result or {}).get("evaluation_status")
                or ("missing_input" if not item else evaluation["status"])
            )
            row.update({
                "dataset_index": case_index + 1,
                "source_line": item.get("source_line") or case_index + 1,
                "group_id": group_id,
                "group_name": variant.get("group_name") or group_id,
                "group_role": variant.get("group_role") or "experiment",
                "group_dataset_name": variant.get("dataset_name") or "",
                "availability": variant.get("availability") or "missing",
                "frames_dir": _project_relative_path(frames[0].parent) if frames else "",
                "evaluation": evaluation,
                "eval_run": {
                    **eval_run,
                    "operation_layout": "multi_group",
                    "evaluation_strategy": case_result.get("evaluation_strategy")
                    or case.get("evaluation_strategy"),
                    "failure_stage": case_result.get("failure_stage"),
                    "input_image_count": case_result.get("input_image_count") or 0,
                    "case_duration_s": case_result.get("duration_s"),
                },
            })
            rows.append(row)
    return rows


def rows_to_jsonl(rows: list[dict]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        for row in rows
    )


def _with_operation_compat(data: dict) -> dict:
    """按读取时兼容旧任务判定，不修改磁盘上的历史快照。"""
    if data.get("mode") != "operation":
        return data
    normalized = dict(data)
    results: list[dict] = []
    for original in data.get("results") or []:
        row = dict(original)
        if "correctness" in row and "issue_types" not in row:
            correctness, issue_types = map_legacy_operation_result(
                row.get("correctness"),
                row.get("error_type"),
            )
            row["correctness"] = correctness
            row["issue_types"] = issue_types
            row.pop("error_type", None)
        results.append(row)
    normalized["results"] = results
    summary = dict(data.get("summary") or {})
    judged = [row for row in results if "error" not in row and row.get("correctness")]
    if judged:
        ok_count = sum(row.get("correctness") == "ok" for row in judged)
        summary.pop("right_count", None)
        summary.pop("accuracy", None)
        summary["ok_count"] = ok_count
        summary["problem_count"] = len(judged) - ok_count
        summary["completion_rate"] = round(ok_count / len(judged), 3)
    if (data.get("options") or {}).get("operation_layout") != "multi_group":
        summary["operation_statistics"] = summarize_operation_results(
            results,
            total_cases=len(data.get("items") or []),
        )
    normalized["summary"] = summary
    return normalized


def export_rows(snapshot: dict, cfg: Any | None = None) -> dict[str, list[dict]]:
    """把一次评测拆成多个 Sheet 的行数据。

    ``数据集明细`` 与 ``逐题结果`` 都以原始 items 为主表，严格按输入顺序
    一一对齐。并发评测导致的完成顺序变化不会影响导出；失败或待评估条目
    仍占据原行，只将评分字段留空。

    逐题结果按维度展开成独立列（维度_X / 理由_X），CSV 与 XLSX 概览
    sheet 均走此格式；任务类使用固定白名单，避免混入其他模块维度和
    内部调试字段。非 compare 模式下仍按垂域分 sheet，便于筛选分析。
    传入 cfg 时，会按 skill 配置保留完整的维度列，N/A 的维度也会导出并在单元格填"N/A"。
    """
    snapshot = _with_operation_compat(snapshot)
    results = _results_with_identity(snapshot)
    aligned_results = _aligned_results(snapshot, results)
    summary = snapshot.get("summary") or {}
    by_skill = summary.get("by_skill") if isinstance(summary.get("by_skill"), dict) else {}
    overview = by_skill.get("overview") or []
    sections = by_skill.get("sections") or []
    mode = snapshot.get("mode")
    multi_operation = (
        mode == "operation"
        and (snapshot.get("options") or {}).get("operation_layout") == "multi_group"
    )

    rows: dict[str, list[dict]] = {
        "数据集明细": (
            _operation_multi_dataset_rows(snapshot)
            if multi_operation
            else _dataset_rows(snapshot, compact_media=mode == "operation")
        ),
        "逐题结果": _result_rows_compact(
            aligned_results,
            _all_dim_names(results, cfg),
        ),
    }
    if mode == "rich_content":
        # 垂域视觉评测：使用中文列名并按固定顺序导出
        rows["逐题结果"] = _rich_content_export_rows(aligned_results)
    elif mode == "operation":
        if multi_operation:
            rows["多组对照"] = _operation_multi_comparison_rows(snapshot)
            rows["逐题结果"] = _operation_multi_result_rows(snapshot)
        else:
            rows["逐题结果"] = _operation_export_rows(
                aligned_results,
                snapshot.get("items") or [],
            )
    frame_rows = _frame_manifest_rows(
        snapshot,
        include_original_video=mode != "operation",
    )
    if frame_rows:
        rows["抽帧清单"] = frame_rows
    rerun_rows = _rerun_record_rows(snapshot)
    if rerun_rows:
        rows["重跑记录"] = rerun_rows
    if mode == "operation":
        # 任务类只有一个固定垂域，不再生成重复的按垂域拆分表、
        # 失败表和通用垂域统计表。失败与告警仍在“逐题结果”原行展示。
        rows["运行汇总"] = [_operation_run_summary(snapshot)]
        if multi_operation and (summary.get("group_summaries") or []):
            rows["实验组汇总"] = list(summary["group_summaries"])
        if not multi_operation:
            rows["统计分布"] = _operation_statistics_export_rows(snapshot)
        return rows
    rows["运行信息"] = [_run_info(snapshot)]
    if mode == "compare":
        rows["逐题结果"] = _result_rows(
            aligned_results,
            _all_dim_names(results, cfg),
        )
    else:
        for name, skill_rows, dim_names in _per_skill_sheets(results, cfg):
            rows[name] = _result_rows(skill_rows, dim_names)
    rows["垂域总览"] = overview
    rows["维度问题占比"] = _dim_problem_rows(sections)
    rows["图表数据"] = _chart_rows(summary)
    if summary:
        rows["汇总指标"] = [_flatten_dict(summary, skip_keys={"by_skill"})]
    return rows


def _results_with_identity(snapshot: dict) -> list[dict]:
    """为历史失败结果回填题号和 query，兼容早期不完整快照。"""
    items = snapshot.get("items") or []
    results: list[dict] = []
    for position, result in enumerate(snapshot.get("results") or []):
        row = dict(result)
        raw_index = row.get("index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        item = items[index] if 0 <= index < len(items) else {}
        if not row.get("item_id"):
            row["item_id"] = item.get("id") or f"q{index}"
        if not row.get("query"):
            row["query"] = item.get("query") or item.get("question") or ""
        row["评估状态"] = "评估失败" if row.get("error") else "已完成"
        results.append(row)
    return results


def _aligned_results(snapshot: dict, results: list[dict]) -> list[dict]:
    """按输入 items 左连接结果；运行中/失败条目也保留固定行位。"""
    items = snapshot.get("items") or []
    if not items:
        return results

    by_index: dict[int, dict] = {}
    by_item_id: dict[str, dict] = {}
    for result in results:
        try:
            index = int(result.get("index"))
        except (TypeError, ValueError):
            index = -1
        if index >= 0:
            by_index[index] = result
        item_id = str(result.get("item_id") or "").strip()
        if item_id:
            by_item_id[item_id] = result

    aligned: list[dict] = []
    progress = snapshot.get("item_progress") or {}
    for index, item in enumerate(items):
        item_id = str(item.get("id") or f"q{index}")
        result = by_index.get(index) or by_item_id.get(item_id)
        if result is not None:
            export_row = {
                "数据集序号": index + 1,
                "index": index,
                "item_id": item_id,
                "query": item.get("query") or item.get("question") or "",
            }
            export_row.update(result)
            aligned.append(export_row)
            continue
        progress_row = progress.get(str(index)) or progress.get(index) or {}
        status = progress_row.get("status")
        export_status = "评估失败" if status == "error" else "待评估"
        aligned.append({
            "数据集序号": index + 1,
            "index": index,
            "item_id": item_id,
            "query": item.get("query") or item.get("question") or "",
            "context": item.get("context") or "",
            "评估状态": export_status,
            "error": progress_row.get("error") or (
                progress_row.get("message") if status == "error" else ""
            ),
        })
    return aligned


_RUNTIME_ITEM_FIELDS = {
    "frames",
    "frame_count",
    "media",
    "video_name",
    "duration",
    "source_data",
}

# 任务类“逐题结果”只保留分析和定位问题所需的字段。
# 字段顺序同时也是 Excel 列顺序。
_OPERATION_EXPORT_COLUMNS = (
    "数据集序号",
    "item_id",
    "index",
    "序号",
    "sessionid",
    "query",
    "video_path",
    "分享链接",
    "context",
    "answer",
    "Provider",
    "Provider ID",
    "模型",
    "Provider版本",
    "task_type",
    "execution_routes",
    "链路类型",
    "route_status",
    "route_evidence",
    "route_rationale",
    "correctness",
    "issue_types",
    "is_low_level",
    "total",
    "维度_操作完成度",
    "理由_操作完成度",
    "维度_步骤正确性",
    "理由_步骤正确性",
    "rationale",
    "latency_s",
    "重跑次数",
    "最后重跑时间",
    "评估状态",
    "error",
    "video_prepare_warnings",
)

_OPERATION_ROUTE_DISPLAY = {
    "fast_system": "快系统",
    "skill": "skill",
    "jarvis": "贾维斯",
    "other": "其他",
}

_OPERATION_GROUP_ROLE_DISPLAY = {
    "control": "对照组",
    "experiment": "实验组",
}


def _operation_group_role_zh(value: Any) -> str:
    raw = str(value or "experiment")
    return _OPERATION_GROUP_ROLE_DISPLAY.get(raw, raw)


def _format_operation_routes_zh(value: Any) -> str:
    """将任务类执行链路转换为适合人工查看的中文文本。"""
    if isinstance(value, str):
        routes = [part.strip() for part in re.split(r"[；;,]", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        routes = [str(part).strip() for part in value if str(part).strip()]
    else:
        routes = []
    return "；".join(_OPERATION_ROUTE_DISPLAY.get(route, route) for route in routes)


def _operation_export_rows(results: list[dict], items: list[dict]) -> list[dict]:
    """将任务类结果转为固定列。

    原始数据集字段由“数据集明细”完整保留；这里只展示任务类的
    核心评估字段。结果缺失时从原始 item 回填输入字段，评分字段留空。
    """
    export: list[dict] = []
    for position, result in enumerate(results):
        item = items[position] if position < len(items) else {}
        source = _source_data_for_item(item)
        item_id = result.get("item_id") or item.get("id") or f"q{position}"
        source_video_path = source.get("video_path")
        if source_video_path is None:
            source_video_path = _project_relative_path(item.get("video_path"))
        session_id = ""
        for session_key in ("sessionid", "session_id", "sessionId"):
            if session_key in source:
                session_id = source.get(session_key)
                break
        rubric = result.get("rubric") or {}
        reasons = result.get("rubric_reasons") or {}
        values = {
            "数据集序号": result.get("数据集序号", position + 1),
            "item_id": item_id,
            "index": (
                source.get("index")
                if "index" in source else item.get("index", "")
            ),
            "序号": _jsonl_sequence(source, item, str(item_id)),
            "sessionid": session_id,
            "query": result.get("query") or item.get("query") or item.get("question") or "",
            "video_path": source_video_path,
            "分享链接": source.get("分享链接", ""),
            "context": result.get("context") or item.get("context") or "",
            "answer": result.get("answer") or item.get("answer") or "",
            "Provider": result.get("judge_provider", ""),
            "Provider ID": result.get("judge_provider_id", ""),
            "模型": result.get("judge_model", ""),
            "Provider版本": result.get("judge_provider_revision", ""),
            "task_type": result.get("task_type", ""),
            "execution_routes": result.get("execution_routes", ""),
            "链路类型": _format_operation_routes_zh(
                result.get("execution_routes", "")
            ),
            "route_status": result.get("route_status", ""),
            "route_evidence": result.get("route_evidence", ""),
            "route_rationale": result.get("route_rationale", ""),
            "correctness": result.get("correctness", ""),
            "issue_types": result.get("issue_types", ""),
            "is_low_level": result.get("is_low_level", ""),
            "total": result.get("total", ""),
            "维度_操作完成度": rubric.get("操作完成度", ""),
            "理由_操作完成度": reasons.get("操作完成度", ""),
            "维度_步骤正确性": rubric.get("步骤正确性", ""),
            "理由_步骤正确性": reasons.get("步骤正确性", ""),
            "rationale": result.get("rationale", ""),
            "latency_s": result.get("latency_s", ""),
            "重跑次数": result.get("rerun_count", 0),
            "最后重跑时间": _format_ts(result.get("last_rerun_at")),
            "评估状态": result.get("评估状态", ""),
            "error": result.get("error", ""),
            "video_prepare_warnings": result.get("video_prepare_warnings", ""),
        }
        for key in ("execution_routes", "issue_types", "video_prepare_warnings"):
            if isinstance(values[key], list):
                values[key] = "；".join(str(value) for value in values[key])
        if isinstance(values["route_evidence"], (list, dict)):
            values["route_evidence"] = json.dumps(
                values["route_evidence"], ensure_ascii=False
            )
        export.append({key: values[key] for key in _OPERATION_EXPORT_COLUMNS})
    return export


def _operation_multi_case_results(snapshot: dict) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    for position, result in enumerate(snapshot.get("results") or []):
        try:
            index = int(result.get("index", position))
        except (TypeError, ValueError):
            index = position
        rows[index] = result
    return rows


def _operation_multi_dataset_rows(snapshot: dict) -> list[dict]:
    """多组数据集明细：每个 case 的每个实验组占一行。"""
    rows: list[dict] = []
    for case_index, case in enumerate(snapshot.get("items") or []):
        for variant in case.get("group_variants") or []:
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            source = _source_data_for_item(item) if item else {}
            frames = [str(path) for path in (item.get("frames") or [])]
            row: dict[str, Any] = {
                "数据集序号": case_index + 1,
                "case_id": case.get("case_id") or case.get("id") or "",
                "实验组": variant.get("group_name") or variant.get("group_id") or "",
                "数据组角色": _operation_group_role_zh(variant.get("group_role")),
                "group_id": variant.get("group_id") or "",
                "数据集文件": variant.get("dataset_name") or "",
                "availability": variant.get("availability") or "missing",
                "id": item.get("id") or "",
                "query": item.get("query") or case.get("query") or "",
            }
            for key, value in source.items():
                row.setdefault(key, value)
            row["录屏项目相对路径"] = _project_relative_path(item.get("video_path"))
            row["抽帧目录项目相对路径"] = (
                _project_relative_path(Path(frames[0]).parent) if frames else ""
            )
            rows.append(row)
    return rows


def _operation_multi_result_rows(snapshot: dict) -> list[dict]:
    """多组逐题结果：按 case × 实验组展开，便于筛选和二次统计。"""
    results_by_index = _operation_multi_case_results(snapshot)
    rows: list[dict] = []
    for case_index, case in enumerate(snapshot.get("items") or []):
        case_result = results_by_index.get(case_index) or {}
        result_by_group = {
            str(row.get("group_id") or ""): row
            for row in (case_result.get("group_results") or [])
        }
        for variant in case.get("group_variants") or []:
            group_id = str(variant.get("group_id") or "")
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            group_result = result_by_group.get(group_id) or {}
            merged = {
                **group_result,
                "数据集序号": case_index + 1,
                "item_id": group_result.get("item_id") or item.get("id") or "",
                "query": group_result.get("query") or item.get("query") or case.get("query") or "",
                "评估状态": (
                    "缺少输入" if not item else
                    "评估失败" if group_result.get("error") else
                    "已完成" if group_result else "待评估"
                ),
            }
            base = _operation_export_rows([merged], [item])[0]
            rows.append({
                "case_id": case.get("case_id") or case.get("id") or "",
                "实验组": variant.get("group_name") or group_id,
                "数据组角色": _operation_group_role_zh(variant.get("group_role")),
                "group_id": group_id,
                "数据集文件": variant.get("dataset_name") or "",
                "availability": variant.get("availability") or "missing",
                "evaluation_strategy": case_result.get("evaluation_strategy")
                or case.get("evaluation_strategy") or "",
                "失败阶段": case_result.get("failure_stage") or "",
                "输入图片数": group_result.get("submitted_image_count") or 0,
                "Case总耗时（秒）": case_result.get("duration_s") or "",
                **base,
            })
    return rows


def _operation_multi_comparison_rows(snapshot: dict) -> list[dict]:
    """每个 case 一行、每个实验组一组关键列，供横向对照。"""
    results_by_index = _operation_multi_case_results(snapshot)
    rows: list[dict] = []
    for case_index, case in enumerate(snapshot.get("items") or []):
        case_result = results_by_index.get(case_index) or {}
        result_by_group = {
            str(row.get("group_id") or ""): row
            for row in (case_result.get("group_results") or [])
        }
        row: dict[str, Any] = {
            "数据集序号": case_index + 1,
            "case_id": case.get("case_id") or case.get("id") or "",
            "query": case.get("query") or "",
            "对齐状态": case.get("alignment_status") or "",
            "对齐警告": "；".join(case.get("alignment_warnings") or []),
            "评估策略": case_result.get("evaluation_strategy")
            or case.get("evaluation_strategy") or "",
            "失败阶段": case_result.get("failure_stage") or "",
            "输入图片总数": case_result.get("input_image_count") or 0,
            "Case总耗时（秒）": case_result.get("duration_s") or "",
        }
        for variant in case.get("group_variants") or []:
            group_id = str(variant.get("group_id") or "")
            group_name = str(variant.get("group_name") or group_id)
            role_name = _operation_group_role_zh(variant.get("group_role"))
            result = result_by_group.get(group_id) or {}
            prefix = f"{role_name}｜{group_name}_"
            row[f"{prefix}correctness"] = result.get("correctness") or ""
            issue_types = result.get("issue_types") or []
            row[f"{prefix}issue_types"] = (
                "；".join(str(value) for value in issue_types)
                if isinstance(issue_types, list) else issue_types
            )
            routes = result.get("execution_routes") or []
            row[f"{prefix}执行链路"] = _format_operation_routes_zh(routes)
            row[f"{prefix}rationale"] = result.get("rationale") or ""
            row[f"{prefix}输入图片数"] = result.get("submitted_image_count") or 0
            row[f"{prefix}模型耗时（秒）"] = result.get("latency_s") or ""
            row[f"{prefix}状态"] = result.get("evaluation_status") or (
                "missing_input" if variant.get("item") is None else "pending"
            )
        rows.append(row)
    return rows


def operation_item_result_row(snapshot: dict, item_index: int) -> dict:
    """返回与任务类 Excel 逐题结果同字段的单题映射，证据保留 JSON 类型。"""
    normalized = _with_operation_compat(snapshot)
    items = normalized.get("items") or []
    if item_index < 0 or item_index >= len(items):
        raise IndexError("item_index 超出数据集范围")

    results = _results_with_identity(normalized)
    aligned = _aligned_results(normalized, results)
    rows = _operation_export_rows(aligned, items)
    row = dict(rows[item_index])

    raw_evidence = aligned[item_index].get("route_evidence") or []
    if isinstance(raw_evidence, str):
        try:
            raw_evidence = json.loads(raw_evidence)
        except (json.JSONDecodeError, TypeError):
            raw_evidence = []
    if not isinstance(raw_evidence, list):
        raw_evidence = []

    evidence: list[dict] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            continue
        route = str(raw_item.get("route") or "")
        evidence.append({
            **raw_item,
            "route": route,
            "route_name": _OPERATION_ROUTE_DISPLAY.get(route, route),
        })
    row["route_evidence"] = evidence
    return row

# 垂域视觉评测（rich_content）Excel/CSV 导出列：与前端展示一致，按此顺序输出
_RICH_CONTENT_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("item_id", "题号"),
    ("query", "Query"),
    ("context", "context"),
    ("category_display", "垂域"),
    ("answer_text", "answer_text"),
    ("card_presence_label", "是否有卡片"),
    ("card_count", "卡片数量"),
    ("card_types", "卡片种类"),
    ("card_contents", "卡片内容"),
    ("superlink_presence_label", "Superlink是否存在"),
    ("superlink_count", "Superlink数量"),
    ("superlink_texts", "Superlink文字"),
    ("card_suitability", "卡片是否合适"),
    ("card_suitability_reason", "卡片不合适原因"),
    ("superlink_suitability", "Superlink是否合适"),
    ("superlink_suitability_reason", "Superlink不合适原因"),
    ("answer_coverage", "回答覆盖"),
    ("needs_review_label", "识别是否需要人工复查"),
    ("review_reason", "需要复核的原因"),
    ("problem_solved", "评价是否解决了用户问题"),
    ("problem_solved_reason", "评价的原因"),
    ("answer_issues", "回答的内容有什么问题"),
    ("rationale", "识别结论"),
    ("latency_s", "耗时"),
    ("rerun_count", "重跑次数"),
    ("last_rerun_at", "最后重跑时间"),
]

# 列表类字段取值后需要拼接为字符串
_RICH_CONTENT_LIST_FIELDS = {"card_types", "card_contents", "superlink_texts"}

# 枚举值 → 展示值映射
_RICH_CONTENT_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "answer_coverage": {"complete": "完整", "partial": "部分", "unclear": "不确定"},
    "card_suitability": {
        "ok": "OK",
        "nok": "NOK",
        "suitable": "合适",
        "partially_suitable": "部分合适",
        "unsuitable": "不合适",
        "unclear": "不确定",
        "not_applicable": "N/A",
    },
    "superlink_suitability": {
        "ok": "OK",
        "nok": "NOK",
        "suitable": "合适",
        "partially_suitable": "部分合适",
        "unsuitable": "不合适",
        "unclear": "不确定",
        "not_applicable": "N/A",
    },
    "problem_solved": {"ok": "OK", "nok": "NOK", "need_review": "需复查"},
}


def _rich_content_export_rows(results: list[dict]) -> list[dict]:
    """将 rich_content 结果行按导出列顺序重排并转换为中文列名，列与前端展示一致。"""
    export: list[dict] = []
    for row in results:
        export_row: dict[str, Any] = {}
        for key, label in _RICH_CONTENT_EXPORT_COLUMNS:
            value = row.get(key)
            if value is None:
                value = ""
            if key in _RICH_CONTENT_LIST_FIELDS and isinstance(value, list):
                value = "；".join(str(v) for v in value)
            if key in _RICH_CONTENT_DISPLAY_MAP and value:
                value = _RICH_CONTENT_DISPLAY_MAP[key].get(str(value), value)
            if key == "last_rerun_at" and value:
                value = _format_ts(value)
            export_row[label] = value
        export.append(export_row)
    return export


def _project_relative_path(
    value: Any,
    project_root: Path | None = None,
) -> str:
    """把项目内路径转为稳定的 POSIX 相对路径；项目外路径返回空。"""
    if value is None or str(value).strip() == "":
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    root = project_root or PROJECT_ROOT
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _source_data_for_item(item: dict) -> dict:
    source = item.get("source_data")
    if isinstance(source, dict):
        return dict(source)
    # 旧历史没有 source_data：尽量从规范化 item 回填，不暴露运行时绝对帧列表。
    return {
        key: value
        for key, value in item.items()
        if key not in _RUNTIME_ITEM_FIELDS
    }


def _dataset_rows(snapshot: dict, *, compact_media: bool = False) -> list[dict]:
    rows: list[dict] = []
    for index, item in enumerate(snapshot.get("items") or []):
        source = _source_data_for_item(item)
        row: dict[str, Any] = {
            "数据集序号": index + 1,
            "source_line": item.get("source_line") or index + 1,
            "id": item.get("id") or f"q{index}",
            "query": item.get("query") or item.get("question") or "",
        }
        for key, value in source.items():
            if key not in row:
                row[key] = value

        frames = [str(path) for path in (item.get("frames") or [])]
        video_runtime_path = item.get("video_path") or (
            (item.get("media") or [""])[0]
        )
        frame_dir = (
            _project_relative_path(Path(frames[0]).parent)
            if frames else ""
        )
        media_fields = {
            "录屏项目相对路径": _project_relative_path(video_runtime_path),
            "抽帧目录项目相对路径": frame_dir,
        }
        if not compact_media:
            frame_project_paths = [
                path for path in (
                    _project_relative_path(frame)
                    for frame in frames
                )
                if path
            ]
            media_fields.update({
                "帧项目相对路径": "\n".join(frame_project_paths),
                "抽帧数量": item.get("frame_count") or len(frames),
                "录屏时长（秒）": item.get("duration") or "",
            })
        row.update(media_fields)
        rows.append(row)
    return rows


def _frame_metadata(frame_dir: Path) -> tuple[dict[int, dict], dict]:
    metadata_path = frame_dir / "keyframes.json"
    if not metadata_path.is_file():
        return {}, {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    selected = {
        int(row.get("index")): row
        for row in (metadata.get("selected") or [])
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }
    return selected, metadata


def _frame_manifest_rows(
    snapshot: dict,
    *,
    include_original_video: bool = True,
) -> list[dict]:
    """生成一帧一行的导出清单；没有成功抽帧的条目也保留一行。"""
    rows: list[dict] = []
    manifest_items: list[tuple[int, dict, dict]] = []
    multi_operation = (
        snapshot.get("mode") == "operation"
        and (snapshot.get("options") or {}).get("operation_layout") == "multi_group"
    )
    for item_index, case in enumerate(snapshot.get("items") or []):
        if multi_operation:
            for variant in case.get("group_variants") or []:
                item = variant.get("item")
                if isinstance(item, dict):
                    manifest_items.append((item_index, item, variant))
        else:
            manifest_items.append((item_index, case, {}))
    for item_index, item, variant in manifest_items:
        if not (
            item.get("video_path")
            or item.get("media")
            or item.get("frames")
            or _source_data_for_item(item).get("video_path")
        ):
            continue
        frames = [Path(str(path)) for path in (item.get("frames") or [])]
        selected, _ = _frame_metadata(frames[0].parent) if frames else ({}, {})
        base = {
            "数据集序号": item_index + 1,
            "实验组": variant.get("group_name") or "",
            "数据组角色": _operation_group_role_zh(variant.get("group_role")),
            "group_id": variant.get("group_id") or "",
            "id": item.get("id") or f"q{item_index}",
            "query": item.get("query") or item.get("question") or "",
            "录屏项目相对路径": _project_relative_path(item.get("video_path")),
        }
        if include_original_video:
            source = _source_data_for_item(item)
            source_video = source.get("video_path") or item.get("video_path") or ""
            base["原始video_path"] = source_video
        if not frames:
            rows.append({
                **base,
                "帧序号": "",
                "帧项目相对路径": "",
                "时间点": "",
                "来源": "",
                "保留原因": "",
                "抽帧状态": "无抽帧结果",
            })
            continue
        for frame_index, frame in enumerate(frames, start=1):
            info = selected.get(frame_index) or {}
            rows.append({
                **base,
                "帧序号": frame_index,
                "帧项目相对路径": _project_relative_path(frame),
                "时间点": info.get("time", ""),
                "来源": info.get("source", ""),
                "保留原因": info.get("keep_reason", ""),
                "抽帧状态": "已生成" if frame.is_file() else "文件缺失",
            })
    return rows


def _skill_dim_names(skill_name: str, cfg: Any | None) -> list[str] | None:
    """根据垂域 skill 名返回该 skill 配置的一级维度名；cfg 未传时返回 None。"""
    if cfg is None:
        return None
    skill = cfg.domain_skills.get(skill_name) if getattr(cfg, "domain_skills", None) else None
    if skill and getattr(skill, "rubrics", None):
        return [d.name for d in skill.rubrics]
    if getattr(cfg, "rubrics", None):
        return [d.name for d in cfg.rubrics]
    return None


def _all_dim_names(results: list[dict], cfg: Any | None) -> list[str] | None:
    """取所有结果涉及 skill 的维度名并集；cfg 未传时返回 None。"""
    if cfg is None or not results:
        return None
    names = set()
    for r in results:
        names.update(_skill_dim_names(r.get("category") or "default", cfg) or [])
    return sorted(names) if names else None


def _run_info(snapshot: dict) -> dict:
    created = snapshot.get("created_at")
    updated = snapshot.get("updated_at")
    return {
        "task_id": snapshot.get("task_id"),
        "dataset_name": snapshot.get("dataset_name") or "",
        "note": snapshot.get("note") or "",
        "mode": snapshot.get("mode"),
        "status": snapshot.get("status"),
        "total": len(snapshot.get("items") or []),
        "done": len([r for r in (snapshot.get("results") or []) if "error" not in r]),
        "created_at": _format_ts(created),
        "updated_at": _format_ts(updated),
        "started_at": _format_ts(snapshot.get("started_at")),
        "finished_at": _format_ts(snapshot.get("finished_at")),
        "duration_s": _stored_timing(snapshot)["duration_s"],
        "options": snapshot.get("options") or {},
        **_judge_backend_summary(snapshot),
        "error": snapshot.get("error") or "",
        "rerun_count": len(snapshot.get("rerun_history") or []),
    }


def _rerun_record_rows(snapshot: dict) -> list[dict]:
    """把批次级重跑审计展开为一题一行，便于 Excel 追溯和筛选。"""
    items = snapshot.get("items") or []
    rows: list[dict] = []
    for attempt in snapshot.get("rerun_history") or []:
        attempt_backend = attempt.get("judge_backend") or {}
        detail_by_index = {
            int(detail["index"]): detail
            for detail in (attempt.get("items") or [])
            if isinstance(detail, dict) and str(detail.get("index", "")).isdigit()
        }
        for raw_index in attempt.get("item_indices") or []:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            item = items[index] if 0 <= index < len(items) else {}
            detail = detail_by_index.get(index, {})
            rows.append({
                "重跑批次": attempt.get("attempt_no", ""),
                "attempt_id": attempt.get("attempt_id", ""),
                "数据集序号": index + 1,
                "item_id": detail.get("item_id") or item.get("id") or f"q{index}",
                "query": item.get("query") or item.get("question") or "",
                "批次状态": attempt.get("status", ""),
                "单题状态": detail.get("status") or (
                    "未执行" if attempt.get("status") in {"cancelled", "interrupted"} else ""
                ),
                "重跑前状态": detail.get("previous_status", ""),
                "correctness": detail.get("correctness", ""),
                "total": detail.get("total", ""),
                "latency_s": detail.get("latency_s", ""),
                "Provider": detail.get("judge_provider") or attempt_backend.get("provider_name") or "角色默认配置",
                "Provider ID": detail.get("judge_provider_id") or attempt_backend.get("provider_id") or "",
                "模型": detail.get("judge_model") or attempt_backend.get("model") or "",
                "Provider版本": detail.get("judge_provider_revision") or attempt_backend.get("provider_revision") or "",
                "开始时间": _format_ts(attempt.get("started_at")),
                "完成时间": _format_ts(detail.get("finished_at") or attempt.get("finished_at")),
                "批次耗时（秒）": attempt.get("duration_s", ""),
                "error": detail.get("error") or attempt.get("error") or "",
            })
    return rows


def _operation_run_summary(snapshot: dict) -> dict:
    """任务类单行运行汇总。

    将通用的“运行信息”和“汇总指标”合并，并把 options 及
    correctness_dist 中常用字段展开，避免 Excel 中出现嵌套 JSON。
    """
    summary = snapshot.get("summary") or {}
    options = snapshot.get("options") or {}
    distribution = summary.get("correctness_dist") or {}
    results = snapshot.get("results") or []
    done = len([row for row in results if "error" not in row])
    failed = len([row for row in results if "error" in row])
    total = len(snapshot.get("items") or [])
    judges = options.get("judges") or []
    if isinstance(judges, list):
        judges = "；".join(str(judge) for judge in judges)
    return {
        "task_id": snapshot.get("task_id"),
        "dataset_name": snapshot.get("dataset_name") or "",
        "note": snapshot.get("note") or "",
        "mode": snapshot.get("mode"),
        "status": snapshot.get("status"),
        "created_at": _format_ts(snapshot.get("created_at")),
        "updated_at": _format_ts(snapshot.get("updated_at")),
        "started_at": _format_ts(snapshot.get("started_at")),
        "finished_at": _format_ts(snapshot.get("finished_at")),
        "duration_s": _stored_timing(snapshot)["duration_s"],
        "rerun_count": len(snapshot.get("rerun_history") or []),
        "judges": judges,
        "model": options.get("model") or "",
        **_judge_backend_summary(snapshot),
        "concurrency": options.get("concurrency", ""),
        "eval_timeout_s": options.get("eval_timeout_s", ""),
        "total": summary.get("total", total),
        "done": summary.get("done", done),
        "failed": summary.get("failed", failed),
        "pending": max(total - done - failed, 0),
        "ok_count": summary.get("ok_count", distribution.get("ok", 0)),
        "nok_count": distribution.get("nok", 0),
        "no_support_count": distribution.get("no_support", 0),
        "others_count": distribution.get("others", 0),
        "problem_count": summary.get("problem_count", ""),
        "completion_rate": summary.get("completion_rate", ""),
        "mean_total": summary.get("mean_total", ""),
        "norm_mean": summary.get("norm_mean", ""),
        "error": snapshot.get("error") or "",
    }


def operation_statistics_payload(snapshot: dict) -> dict:
    """构建供 API、Web 和 Excel 共用的任务类统计 JSON。"""
    if snapshot.get("mode") != "operation":
        raise ValueError("仅任务类评估支持统计分布")
    if (snapshot.get("options") or {}).get("operation_layout") == "multi_group":
        raise ValueError("任务类多组评估的统计口径尚未启用")
    normalized = _with_operation_compat(snapshot)
    results = _results_with_identity(normalized)
    aligned = _aligned_results(normalized, results)
    return {
        "schema_version": 1,
        "task_id": normalized.get("task_id") or "",
        "dataset_name": normalized.get("dataset_name") or "",
        "mode": "operation",
        "statistics": summarize_operation_results(
            aligned,
            total_cases=len(normalized.get("items") or []),
        ),
    }


def operation_comparison_batch(snapshot: dict) -> dict:
    """将普通任务类历史快照转换为批次对比所需的紧凑输入。"""
    if snapshot.get("mode") != "operation":
        raise ValueError("仅任务类历史支持批次对比")
    if (snapshot.get("options") or {}).get("operation_layout") == "multi_group":
        raise ValueError("任务类多组评估历史暂不参与批次对比")
    normalized = _with_operation_compat(snapshot)
    items = normalized.get("items") or []
    results = _results_with_identity(normalized)
    aligned = _aligned_results(normalized, results)
    export_rows = _operation_export_rows(aligned, items)
    rows = []
    for position, item in enumerate(items):
        source = _source_data_for_item(item)
        match_index = (
            source.get("index")
            if "index" in source else item.get("index", "")
        )
        export_row = dict(export_rows[position]) if position < len(export_rows) else {}
        export_row["index"] = match_index
        export_row["case_id"] = item.get("case_id") or source.get("case_id") or ""
        for video_url_key in ("录屏URL", "录屏url", "video_url", "视频链接"):
            if source.get(video_url_key):
                export_row["录屏URL"] = source[video_url_key]
                break
        rows.append({
            "position": position,
            "index": match_index,
            "item_id": item.get("id") or f"q{position}",
            "case_id": item.get("case_id") or source.get("case_id") or "",
            "query": item.get("query") or item.get("question") or "",
            "result": aligned[position] if position < len(aligned) else {},
            "export": export_row,
        })
    backend = _judge_backend_summary(normalized)
    return {
        "task_id": normalized.get("task_id") or "",
        "dataset_name": normalized.get("dataset_name") or "",
        "created_at": normalized.get("created_at"),
        "judge_provider": backend["judge_provider"],
        "judge_model": backend["judge_model"],
        "rows": rows,
    }


def _operation_statistics_export_rows(snapshot: dict) -> list[dict]:
    """统计分布的结构化行；XLSX 会将其渲染为同 Sheet 内的两张表。"""
    statistics = operation_statistics_payload(snapshot)["statistics"]
    rows: list[dict] = []
    for row in statistics["correctness_rows"]:
        rows.append({
            "统计类型": "Correctness 分布",
            "类别": row["correctness"],
            "频次": row["count"],
            "占有效评估比例": row["rate"],
        })
    for row in statistics["issue_type_rows"]:
        rows.append({
            "统计类型": "Issue Type 分布",
            "类别": row["issue_type"],
            "频次": row["case_count"],
            "占有效评估比例": row["rate"],
        })
    if not rows:
        rows.append({
            "统计类型": "统计说明",
            "类别": statistics["conclusion"],
            "频次": "",
            "占有效评估比例": "",
        })
    return rows


def _display_percent(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _operation_statistics_sheet(payload: dict) -> tuple[list[list[Any]], set[int], list[int]]:
    """把统计 JSON 渲染为统计 Sheet 的矩阵、加粗行号和列宽。"""
    statistics = payload["statistics"]
    matrix: list[list[Any]] = [
        ["任务类评估统计"],
        ["数据集名称", payload.get("dataset_name") or ""],
        ["数据集总量", statistics["total_cases"]],
        ["有效评估数", statistics["valid_count"]],
        ["评估失败数", statistics["failed_count"]],
        ["待评估数", statistics["pending_count"]],
        ["评估覆盖率", _display_percent(statistics["coverage_rate"])],
        ["OK 率（有效评估口径）", _display_percent(statistics["ok_rate"])],
        ["OK 率分母（有效评估数）", statistics["ok_rate_denominator"]],
        [],
        ["Correctness 分布"],
        ["判定", "频次", "占有效评估比例"],
    ]
    for row in statistics["correctness_rows"]:
        matrix.append([
            row["correctness"],
            row["count"],
            _display_percent(row["rate"]),
        ])
    matrix.extend([
        [],
        ["Issue Type 分布"],
        ["问题类型", "涉及 Case 数", "占有效 Case 比例"],
    ])
    if statistics["issue_type_rows"]:
        for row in statistics["issue_type_rows"]:
            matrix.append([
                row["issue_type"],
                row["case_count"],
                _display_percent(row["rate"]),
            ])
    else:
        matrix.append(["暂无问题类型", 0, "0.00%"])
    matrix.extend([
        [],
        ["统计结论"],
        [statistics["conclusion"]],
        [],
        ["口径说明"],
        ["OK 率、Correctness 和问题类型占比均以具有合法 correctness 的全部有效评估 Case 为分母。同一 Case 的同一问题类型只计一次，问题类型占比之和可能超过 100%。"],
    ])
    # 行号从 1 开始，与 OOXML 一致。
    bold_rows = {1, 11, 12, 17, 18, len(matrix) - 4, len(matrix) - 1}
    return matrix, bold_rows, [30, 14, 18]


def _format_ts(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _result_rows(results: list[dict], dim_names: list[str] | None = None) -> list[dict]:
    """把维度展开成列；dim_names 为 None 时按每行实际 rubric 生成列。

    dim_names 传入后，所有行都会保留这些维度列（便于 Excel 统一表头）。
    维度若在该行 na_dimensions 中则填 "N/A"，否则没有分数的填空字符串。
    每个维度同时输出 分(维度_X) 和 打分理由(理由_X) 两列。
    """
    rows = []
    for r in results:
        row = dict(r)
        if isinstance(row.get("issue_types"), list):
            row["issue_types"] = "；".join(str(value) for value in row["issue_types"])
        if isinstance(row.get("execution_routes"), list):
            row["execution_routes"] = "；".join(
                str(value) for value in row["execution_routes"]
            )
        if isinstance(row.get("route_evidence"), (list, dict)):
            row["route_evidence"] = json.dumps(
                row["route_evidence"], ensure_ascii=False
            )
        rubric = row.pop("rubric", {}) or {}
        reasons = row.pop("rubric_reasons", {}) or {}
        na_dims = set(r.get("na_dimensions") or [])
        if dim_names is None:
            for dim, score in rubric.items():
                row[f"维度_{dim}"] = score
                row[f"理由_{dim}"] = reasons.get(dim, "")
        else:
            for dim in dim_names:
                if dim in na_dims:
                    row[f"维度_{dim}"] = "N/A"
                    row[f"理由_{dim}"] = ""
                else:
                    row[f"维度_{dim}"] = rubric.get(dim, "")
                    row[f"理由_{dim}"] = reasons.get(dim, "")
        rows.append(row)
    return rows


def _result_rows_compact(results: list[dict], dim_names: list[str] | None = None) -> list[dict]:
    """逐题概览：维度同样展开成独立列，便于 Excel/CSV 中按维度筛选、统计。"""
    return _result_rows(results, dim_names)


def _per_skill_sheets(results: list[dict], cfg: Any | None = None) -> list[tuple[str, list[dict], list[str] | None]]:
    """按垂域分组返回 [(sheet名, 行数据, 该 sheet 的维度列表)]；
    同垂域维度一致可展开列，评估失败的题单独成 sheet 并使用所有失败题涉及维度的并集。
    """
    groups: dict[str, dict] = {}
    failed: list[dict] = []
    for r in results:
        if "error" in r:
            failed.append(r)
            continue
        cat = r.get("category") or "default"
        disp = r.get("category_display") or cat
        groups.setdefault(cat, {"display": disp, "rows": []})["rows"].append(r)
    out = []
    for cat, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["rows"])):
        dim_names = _skill_dim_names(cat, cfg)
        out.append((_sheet_name(f"逐题-{g['display']}"), g["rows"], dim_names))
    if failed:
        out.append(("评估失败", failed, _all_dim_names(failed, cfg)))
    return out


def _dim_problem_rows(sections: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for section in sections:
        for dim, info in (section.get("dim_problem_dist") or {}).items():
            rows.append({
                "skill": section.get("skill"),
                "垂域": section.get("display"),
                "维度": dim,
                "问题题数": info.get("count", len(info.get("item_ids") or [])),
                "占比": info.get("rate"),
                "样例题号": ", ".join(map(str, info.get("item_ids") or [])),
                "样本量": section.get("n_items"),
            })
    return rows


def _chart_rows(summary: dict) -> list[dict]:
    """Excel 图表专用数据源。

    采用宽表而不是“图表/名称/值”长表，方便 OOXML chart 直接引用连续区域。
    """
    by_skill = summary.get("by_skill") if isinstance(summary.get("by_skill"), dict) else {}
    pie_rows = [
        {"垂域": row.get("display"), "样本量": row.get("n_items")}
        for row in (by_skill.get("overview") or [])
        if row.get("n_items", 0) > 0
    ]
    bar_rows = []
    for section in by_skill.get("sections") or []:
        for dim, info in (section.get("dim_problem_dist") or {}).items():
            if (info.get("rate") or 0) <= 0:
                continue
            bar_rows.append({
                "维度问题": f"{section.get('display')} - {dim}",
                "占比": info.get("rate"),
                "问题题数": info.get("count"),
            })

    rows = []
    for i in range(max(len(pie_rows), len(bar_rows))):
        row = {}
        if i < len(pie_rows):
            row.update(pie_rows[i])
        if i < len(bar_rows):
            row.update(bar_rows[i])
        rows.append(row)
    return rows


def _flatten_dict(data: dict, skip_keys: set[str] | None = None) -> dict:
    skip_keys = skip_keys or set()
    return {k: v for k, v in data.items() if k not in skip_keys}


def rows_to_csv(rows: list[dict]) -> str:
    import csv
    from io import StringIO

    out = StringIO()
    keys = _headers(rows)
    writer = csv.DictWriter(out, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in keys})
    return out.getvalue()


def write_frames_zip(
    snapshot: dict,
    destination: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    item_indexes: set[int] | None = None,
) -> Path:
    """将已有关键帧和映射清单打包到磁盘，避免大批量导出占用内存。"""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    source_items = snapshot.get("items") or []
    multi_operation = (
        snapshot.get("mode") == "operation"
        and (snapshot.get("options") or {}).get("operation_layout") == "multi_group"
    )
    items: list[dict] = []
    for source_index, source_item in enumerate(source_items):
        if multi_operation:
            for variant in source_item.get("group_variants") or []:
                group_item = variant.get("item")
                if not isinstance(group_item, dict):
                    continue
                items.append({
                    **group_item,
                    "_export_source_index": source_index,
                    "_export_group_id": variant.get("group_id") or "",
                    "_export_group_name": variant.get("group_name") or "",
                    "_export_group_role": variant.get("group_role") or "experiment",
                })
        else:
            items.append({**source_item, "_export_source_index": source_index})
    width = max(3, len(str(max(len(items), 1))))

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item_index, item in enumerate(items):
            source_index = int(item.get("_export_source_index", item_index))
            if item_indexes is not None and source_index not in item_indexes:
                continue
            sequence = str(item_index + 1).zfill(width)
            raw_id = str(item.get("id") or f"q{item_index + 1}")
            safe_id = _safe_name(raw_id).strip("_")[:100] or f"q{item_index + 1}"
            group_id = str(item.get("_export_group_id") or "")
            group_name = str(item.get("_export_group_name") or "")
            group_role = str(item.get("_export_group_role") or "")
            item_dir = f"{sequence}_{_safe_name(group_id)}_{safe_id}" if group_id else f"{sequence}_{safe_id}"
            frames = [Path(str(path)) for path in (item.get("frames") or [])]
            selected, metadata = (
                _frame_metadata(frames[0].parent) if frames else ({}, {})
            )
            source = _source_data_for_item(item)
            source_video = source.get("video_path") or ""
            if not frames:
                manifest.append({
                    "dataset_index": source_index + 1,
                    "group_id": group_id,
                    "group_name": group_name,
                    "group_role": group_role,
                    "id": raw_id,
                    "query": item.get("query") or item.get("question") or "",
                    "source_video_path": source_video,
                    "video_project_path": _project_relative_path(
                        item.get("video_path"),
                        project_root,
                    ),
                    "frame_index": None,
                    "frame_path": "",
                    "source_frame_project_path": "",
                    "timestamp": None,
                    "keep_reason": "",
                    "status": "missing",
                })
                continue

            for frame_index, frame in enumerate(frames, start=1):
                info = selected.get(frame_index) or {}
                archive_frame = f"{item_dir}/{frame.name}"
                exists = frame.is_file()
                if exists:
                    zf.write(frame, archive_frame)
                manifest.append({
                    "dataset_index": source_index + 1,
                    "group_id": group_id,
                    "group_name": group_name,
                    "group_role": group_role,
                    "id": raw_id,
                    "query": item.get("query") or item.get("question") or "",
                    "source_video_path": source_video,
                    "video_project_path": _project_relative_path(
                        item.get("video_path"),
                        project_root,
                    ),
                    "frame_index": frame_index,
                    "frame_path": archive_frame if exists else "",
                    "source_frame_project_path": _project_relative_path(
                        frame,
                        project_root,
                    ),
                    "timestamp": info.get("time"),
                    "source": info.get("source", ""),
                    "keep_reason": info.get("keep_reason", ""),
                    "status": "ok" if exists else "missing",
                })

            if metadata:
                exported_metadata = dict(metadata)
                exported_metadata["video"] = _project_relative_path(
                    item.get("video_path"),
                    project_root,
                )
                zf.writestr(
                    f"{item_dir}/keyframes.json",
                    json.dumps(exported_metadata, ensure_ascii=False, indent=2),
                )

        manifest_text = "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in manifest
        )
        zf.writestr("manifest.jsonl", manifest_text)
    return target


def load_item_judge_calls(
    snapshot: dict,
    item_index: int,
    *,
    runs_dir: Path = RUNS_DIR,
    project_root: Path = PROJECT_ROOT,
    trace_paths: list[Path] | None = None,
) -> dict:
    """查找单条 case 对应的全部 judge_calls，并组装为可下载 JSON。

    以 task_id + item_index 为主键，item_id 仅作兼容校验，避免 query 重复时
    错配。新任务优先使用快照记录的任务级路径；旧任务继续兼容 runs 根目录
    的全局 ``judge_calls*.jsonl``。
    """
    items = snapshot.get("items") or []
    if item_index < 0 or item_index >= len(items):
        raise IndexError("item_index 超出数据集范围")
    item = items[item_index]
    task_id = str(snapshot.get("task_id") or "")
    session_name = str(snapshot.get("session_name") or "")
    item_id = str(item.get("id") or f"q{item_index}")

    candidates: list[Path] = []
    if trace_paths is not None:
        candidates.extend(Path(path) for path in trace_paths)
    else:
        stored_trace = str(snapshot.get("judge_trace_path") or "").strip()
        if stored_trace:
            stored_path = Path(stored_trace).expanduser()
            if not stored_path.is_absolute():
                stored_path = project_root / stored_path
            candidates.append(stored_path)

        configured_task_path = configured_task_trace_path(task_id, session_name)
        if configured_task_path is not None:
            candidates.append(configured_task_path)

        legacy_path = configured_legacy_trace_path()
        if legacy_path is not None:
            candidates.append(legacy_path)

        # 兼容旧版本在 runs 根目录生成的一个或多个全局日志文件。
        candidates.extend(sorted(runs_dir.glob("judge_calls*.jsonl")))
        # 没有任务级定位信息时才执行旧式递归兜底，避免正常导出扫描整棵 runs。
        if not stored_trace and configured_task_path is None:
            candidates.extend(sorted(runs_dir.rglob("judge_calls*.jsonl")))

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(candidate)

    records: list[dict] = []
    task_needle = f'"{task_id}"' if task_id else ""
    for path in unique_candidates:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig") as file:
                for line in file:
                    if task_needle and task_needle not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if task_id and str(record.get("task_id") or "") != task_id:
                        continue
                    if not task_id and session_name:
                        if str(record.get("session_name") or "") != session_name:
                            continue
                    try:
                        record_index = int(record.get("item_index"))
                    except (TypeError, ValueError):
                        record_index = -1
                    if record_index != item_index:
                        continue
                    record_item_id = str(record.get("item_id") or "")
                    if record_item_id and record_item_id != item_id:
                        continue
                    exported = dict(record)
                    exported["_trace_file"] = _project_relative_path(
                        path,
                        project_root,
                    ) or path.name
                    records.append(exported)
        except OSError:
            continue

    def record_sort_key(row: dict) -> tuple[str, str, int]:
        try:
            round_index = int(row.get("round") or 0)
        except (TypeError, ValueError):
            round_index = 0
        return (
            str(row.get("ts") or ""),
            str(row.get("judge") or ""),
            round_index,
        )

    records.sort(key=record_sort_key)
    return {
        "task_id": snapshot.get("task_id"),
        "session_name": snapshot.get("session_name"),
        "dataset_name": snapshot.get("dataset_name") or "",
        "dataset_index": item_index + 1,
        "item_index": item_index,
        "item_id": item_id,
        "query": item.get("query") or item.get("question") or "",
        "judge_call_count": len(records),
        "judge_calls": records,
    }


def build_xlsx(snapshot: dict, cfg: Any | None = None) -> bytes:
    """生成 xlsx（纯数据 sheet，不含图表）。

    手写 OOXML chart 易被 Excel 判"需修复"且样式差，故不再导出图表——
    图表请看 web 端 ECharts；如需在 Excel 画图，用"图表数据"sheet 自行插入。
    """
    sheets = {name: rows for name, rows in export_rows(snapshot, cfg).items() if rows}
    if not sheets:
        sheets = {"逐题结果": []}

    names = list(sheets)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook_xml(names))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _styles_xml())
        for i, (name, rows) in enumerate(sheets.items(), start=1):
            if name == "统计分布" and snapshot.get("mode") == "operation":
                statistics_payload = operation_statistics_payload(snapshot)
                matrix, bold_rows, widths = _operation_statistics_sheet(
                    statistics_payload
                )
                sheet_xml = _matrix_sheet_xml(
                    matrix,
                    bold_rows=bold_rows,
                    widths=widths,
                )
            else:
                sheet_xml = _sheet_xml(rows)
            zf.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml)
    return buf.getvalue()


def build_operation_comparison_xlsx(payload: dict) -> bytes:
    """导出统一交集统计和逐题横向并集。"""
    groups = payload.get("groups") or []
    overview = [
        ["任务类结果集对比"],
        [
            "统计口径",
            "Correctness 与 Issue Type 仅统计所有选中批次共有且均有效的 Case；"
            "相对对照组表按每个实验组与对照组各自的共同有效 Case 计算；"
            "“其他”表示 nok、no_support 或 others。",
        ],
        ["全组共同 Case", payload.get("all_groups_common_matched_count", 0)],
        ["全组共同有效 Case", payload.get("all_groups_common_valid_count", 0)],
        [],
        ["组别", "数据集", "task_id", "原始数据量", "共有有效数据量", "ok", "nok", "no_support", "others", "OK率"],
    ]
    for group in groups:
        statistics = group.get("statistics") or {}
        correctness = {
            row.get("correctness"): row.get("count", 0)
            for row in statistics.get("correctness_rows") or []
        }
        overview.append([
            group.get("group_label") or "",
            group.get("group_name") or "",
            group.get("task_id") or "",
            group.get("original_count", 0),
            group.get("common_valid_count", 0),
            correctness.get("ok", 0),
            correctness.get("nok", 0),
            correctness.get("no_support", 0),
            correctness.get("others", 0),
            _display_percent(statistics.get("ok_rate")),
        ])
    group_header_row = 6
    group_data_start = group_header_row + 1
    group_data_end = len(overview)
    overview.append([])
    pair_section_row = len(overview) + 1
    overview.append(["相对对照组的共同 Case 对比"])
    pair_header_row = len(overview) + 1
    overview.append(["对比关系", "共同 Case", "共同有效 Case", "对照组OK数", "对照组OK率", "实验组OK数", "实验组OK率", "其他→OK", "OK→其他", "OK净变化", "OK率差值", "结论"])
    pair_data_start = pair_header_row + 1
    for pair in payload.get("pairwise") or []:
        overview.append([
            f"{pair.get('baseline_label') or '对照组'} / {pair.get('target_label') or '实验组'}",
            pair.get("matched_count", 0),
            pair.get("valid_pair_count", 0),
            pair.get("baseline_ok_count", 0),
            _display_percent(pair.get("baseline_ok_rate")),
            pair.get("target_ok_count", 0),
            _display_percent(pair.get("target_ok_rate")),
            pair.get("to_ok_count", 0),
            pair.get("from_ok_count", 0),
            pair.get("net_ok_change", 0),
            _display_signed_percent(pair.get("ok_rate_delta")),
            pair.get("ok_rate_change_label") or "无有效数据",
        ])
    pair_data_end = len(overview)
    overview.append([])
    conclusion_header_row = len(overview) + 1
    overview.append(["统计结论"])
    conclusion_lines = str(payload.get("conclusion") or "").splitlines()
    conclusion_start = len(overview) + 1
    overview.extend([[line] for line in conclusion_lines])

    issue_rows = []
    for pair in payload.get("pairwise") or []:
        for row in pair.get("issue_type_rows") or []:
            issue_rows.append({
                "对比关系": f"{pair.get('baseline_label') or '对照组'} / {pair.get('target_label') or '实验组'}",
                "实验组": pair.get("target_label") or "",
                "共同有效Case": pair.get("valid_pair_count", 0),
                "Issue Type": row.get("issue_type") or "",
                "对照组频次": row.get("baseline_count", 0),
                "对照组占比": row.get("baseline_rate"),
                "实验组频次": row.get("target_count", 0),
                "实验组占比": row.get("target_rate"),
                "频次差值": row.get("count_delta", 0),
                "占比差值": (
                    float(row["rate_delta"]) * 100
                    if row.get("rate_delta") is not None else None
                ),
            })

    detail_fields = (
        "item_id",
        "index",
        "序号",
        "case_id",
        "query",
        "sessionid",
        "answer",
        "context",
        "is_low_level",
        "correctness",
        "issue_types",
        "execution_routes",
        "链路类型",
        "rationale",
        "分享链接",
        "video_path",
        "录屏URL",
        "error",
    )
    union_rows = []
    for row in payload.get("union_rows") or []:
        output = {
            "匹配键": row.get("match_key") or "",
            "存在组数": row.get("present_group_count", 0),
            "所有组共有": "是" if row.get("all_groups_present") else "否",
        }
        source_rows = row.get("group_rows") or {}
        for group in groups:
            source = source_rows.get(group.get("task_id"), {})
            label = group.get("group_label") or ""
            for field in detail_fields:
                value = source.get(field, "")
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                output[f"{label}_{field}"] = value
        union_rows.append(output)

    overview_cell_styles: dict[tuple[int, int], int] = {
        (2, 1): 8,
        (2, 2): 9,
        (3, 1): 8,
        (3, 2): 3,
        (4, 1): 8,
        (4, 2): 3,
    }
    for row_index in range(group_data_start, group_data_end + 1):
        for column_index in range(1, 11):
            overview_cell_styles[(row_index, column_index)] = 3
    for row_index in range(pair_data_start, pair_data_end + 1):
        for column_index in range(1, 13):
            overview_cell_styles[(row_index, column_index)] = 3
    valid_group_rates = [
        (group.get("statistics") or {}).get("ok_rate")
        for group in groups
        if (group.get("statistics") or {}).get("ok_rate") is not None
    ]
    if valid_group_rates:
        best_rate = max(valid_group_rates)
        for offset, group in enumerate(groups):
            rate = (group.get("statistics") or {}).get("ok_rate")
            if rate is not None and abs(float(rate) - float(best_rate)) < 1e-12:
                overview_cell_styles[(group_data_start + offset, 10)] = 4
    for offset, pair in enumerate(payload.get("pairwise") or []):
        row_index = pair_data_start + offset
        change = pair.get("ok_rate_change")
        change_style = 4 if change == "improved" else 5 if change == "worsened" else 6
        overview_cell_styles[(row_index, 11)] = change_style
        overview_cell_styles[(row_index, 12)] = change_style
        net_change = int(pair.get("net_ok_change") or 0)
        overview_cell_styles[(row_index, 10)] = (
            4 if net_change > 0 else 5 if net_change < 0 else 6
        )
    overview_merges = [
        "A1:L1",
        "B2:L2",
        f"A{pair_section_row}:L{pair_section_row}",
        f"A{conclusion_header_row}:L{conclusion_header_row}",
    ]
    for row_index in range(conclusion_start, conclusion_start + len(conclusion_lines)):
        overview_cell_styles[(row_index, 1)] = 9
        overview_merges.append(f"A{row_index}:L{row_index}")

    sheets: list[tuple[str, str]] = [
        ("对比概览", _matrix_sheet_xml(
            overview,
            widths=[22, 46, 22, 14, 16, 12, 12, 12, 14, 14, 14, 12],
            row_styles={
                1: 7,
                group_header_row: 2,
                pair_section_row: 8,
                pair_header_row: 2,
                conclusion_header_row: 8,
            },
            cell_styles=overview_cell_styles,
            merge_refs=overview_merges,
            row_heights={1: 28, 2: 34, group_header_row: 26, pair_header_row: 30},
            freeze_rows=group_header_row,
            hide_gridlines=True,
        )),
        ("Issue Type对比", _operation_comparison_issue_sheet(issue_rows)),
        ("逐题横向对比", _operation_comparison_detail_sheet(
            groups,
            union_rows,
            detail_fields,
        )),
    ]
    return _build_xlsx_xml_sheets(sheets)


def _operation_comparison_issue_sheet(rows: list[dict[str, Any]]) -> str:
    """生成带边框、冻结表头和优化/劣化配色的 Issue Type 表。"""
    headers = [
        "对比关系",
        "实验组",
        "共同有效Case",
        "Issue Type",
        "对照组频次",
        "对照组占比",
        "实验组频次",
        "实验组占比",
        "频次差值",
        "占比差值",
    ]
    table = [headers] + [[row.get(header) for header in headers] for row in rows]
    cell_styles: dict[tuple[int, int], int] = {}
    for row_index, row in enumerate(rows, start=2):
        for column_index in range(1, len(headers) + 1):
            cell_styles[(row_index, column_index)] = 3
        for column_index in (6, 8):
            cell_styles[(row_index, column_index)] = 10
        count_delta = float(row.get("频次差值") or 0)
        rate_delta = float(row.get("占比差值") or 0)
        cell_styles[(row_index, 9)] = 4 if count_delta < 0 else 5 if count_delta > 0 else 6
        cell_styles[(row_index, 10)] = 11 if rate_delta < 0 else 12 if rate_delta > 0 else 13
    return _matrix_sheet_xml(
        table,
        widths=[22, 14, 16, 36, 14, 14, 14, 14, 20, 20],
        row_styles={1: 2},
        cell_styles=cell_styles,
        row_heights={1: 32},
        auto_filter_ref=(f"A1:J{len(table)}" if headers else None),
        freeze_rows=1,
        freeze_columns=4,
        hide_gridlines=True,
    )


def _operation_comparison_detail_sheet(
    groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    detail_fields: tuple[str, ...],
) -> str:
    """生成按数据集合并分组的双层表头逐题对比表。"""
    common_headers = ["匹配键", "存在组数", "所有组共有"]
    group_header = ["公共字段", "", ""]
    field_header = list(common_headers)
    widths = [28, 12, 14]
    field_widths = {
        "item_id": 20,
        "index": 16,
        "序号": 14,
        "case_id": 18,
        "query": 36,
        "sessionid": 20,
        "answer": 36,
        "context": 28,
        "is_low_level": 14,
        "correctness": 14,
        "issue_types": 24,
        "execution_routes": 28,
        "链路类型": 18,
        "rationale": 44,
        "分享链接": 24,
        "video_path": 34,
        "录屏URL": 24,
        "error": 36,
    }
    merge_refs = ["A1:C1"]
    column_index = len(common_headers) + 1
    for group in groups:
        label = group.get("group_label") or "结果组"
        dataset_name = _comparison_dataset_display_name(
            group.get("group_name") or group.get("task_id") or "未命名数据集"
        )
        group_header.extend([f"{label}：{dataset_name}"] + [""] * (len(detail_fields) - 1))
        field_header.extend(detail_fields)
        widths.extend(field_widths.get(field, 18) for field in detail_fields)
        end_column = column_index + len(detail_fields) - 1
        merge_refs.append(f"{_col(column_index)}1:{_col(end_column)}1")
        column_index = end_column + 1

    table = [group_header, field_header]
    for row in rows:
        values = [row.get(header, "") for header in common_headers]
        for group in groups:
            label = group.get("group_label") or ""
            values.extend(row.get(f"{label}_{field}", "") for field in detail_fields)
        table.append(values)

    last_column = _col(len(field_header))
    return _matrix_sheet_xml(
        table,
        widths=widths,
        row_styles={1: 8, 2: 15},
        default_style=14,
        merge_refs=merge_refs,
        row_heights={1: 28, 2: 28},
        auto_filter_ref=f"A2:{last_column}{len(table)}",
        freeze_rows=2,
        freeze_columns=3,
        hide_gridlines=True,
    )


def _comparison_dataset_display_name(value: Any) -> str:
    """移除数据文件后缀，同时保留名称中的版本号等点号。"""
    name = str(value or "").strip()
    suffix = Path(name).suffix.lower()
    if suffix in {".jsonl", ".json", ".csv", ".xlsx", ".xls"}:
        return name[:-len(suffix)]
    return name


def _display_signed_percent(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value) * 100:+.2f}pp"
    except (TypeError, ValueError):
        return str(value)


def _build_xlsx_xml_sheets(sheets: list[tuple[str, str]]) -> bytes:
    names = [name for name, _xml in sheets]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook_xml(names))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _styles_xml())
        for index, (_name, sheet_xml) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)
    return buf.getvalue()


def _headers(rows: list[dict]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    return keys


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _content_types(sheet_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _workbook_xml(names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(_sheet_name(name))}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\*:/\\?]", "_", name)
    return cleaned[:31] or "Sheet"


def _workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    rels += (
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="0.00%"/>'
        '<numFmt numFmtId="165" formatCode="+0.00&quot;pp&quot;;-0.00&quot;pp&quot;;0.00&quot;pp&quot;"/>'
        '</numFmts>'
        '<fonts count="7">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="16"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FF166534"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FF991B1B"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><color rgb="FF64748B"/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="10">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFDBEAFE"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFDCFCE7"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1E3A8A"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color rgb="FFCBD5E1"/></left><right style="thin"><color rgb="FFCBD5E1"/></right><top style="thin"><color rgb="FFCBD5E1"/></top><bottom style="thin"><color rgb="FFCBD5E1"/></bottom><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="16">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="4" fillId="5" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="5" fillId="6" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="6" fillId="7" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="8" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="9" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="4" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="165" fontId="4" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="165" fontId="5" fillId="6" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="165" fontId="6" fillId="7" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="0"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="0"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _sheet_xml(
    rows: list[dict],
    *,
    auto_filter: bool = False,
    bordered: bool = False,
    freeze_rows: int = 0,
    freeze_columns: int = 0,
    hide_gridlines: bool = False,
) -> str:
    headers = _headers(rows)
    table = [headers] + [[row.get(h) for h in headers] for row in rows]
    return _matrix_sheet_xml(
        table,
        bold_rows={1} if not bordered else set(),
        row_styles={1: 2} if bordered else None,
        default_style=3 if bordered else 0,
        widths=[_width(header) for header in headers],
        auto_filter_ref=(
            f"A1:{_col(len(headers))}{len(table)}"
            if auto_filter and headers else None
        ),
        freeze_rows=freeze_rows,
        freeze_columns=freeze_columns,
        hide_gridlines=hide_gridlines,
    )


def _matrix_sheet_xml(
    table: list[list[Any]],
    *,
    bold_rows: set[int] | None = None,
    widths: list[int] | None = None,
    auto_filter_ref: str | None = None,
    default_style: int = 0,
    row_styles: dict[int, int] | None = None,
    cell_styles: dict[tuple[int, int], int] | None = None,
    merge_refs: list[str] | None = None,
    row_heights: dict[int, float] | None = None,
    freeze_rows: int = 0,
    freeze_columns: int = 0,
    hide_gridlines: bool = False,
) -> str:
    """生成支持标题、空行及多段表头的简单工作表。"""
    bold_rows = bold_rows or set()
    row_styles = row_styles or {}
    cell_styles = cell_styles or {}
    row_heights = row_heights or {}
    rows_xml = []
    for r_idx, row in enumerate(table, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = f"{_col(c_idx)}{r_idx}"
            style_id = cell_styles.get(
                (r_idx, c_idx),
                row_styles.get(r_idx, 1 if r_idx in bold_rows else default_style),
            )
            style = f' s="{style_id}"' if style_id else ""
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            ):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(_cell(value))}</t></is></c>')
        height = row_heights.get(r_idx)
        height_attr = f' ht="{height}" customHeight="1"' if height else ""
        rows_xml.append(f'<row r="{r_idx}"{height_attr}>{"".join(cells)}</row>')
    max_columns = max((len(row) for row in table), default=0)
    resolved_widths = list(widths or [])
    if len(resolved_widths) < max_columns:
        resolved_widths.extend([18] * (max_columns - len(resolved_widths)))
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
        for i, width in enumerate(resolved_widths[:max_columns], start=1)
    )
    auto_filter = (
        f'<autoFilter ref="{escape(auto_filter_ref)}"/>'
        if auto_filter_ref else ""
    )
    merge_cells = ""
    if merge_refs:
        merge_cells = (
            f'<mergeCells count="{len(merge_refs)}">'
            + "".join(f'<mergeCell ref="{escape(ref)}"/>' for ref in merge_refs)
            + "</mergeCells>"
        )
    show_gridlines = ' showGridLines="0"' if hide_gridlines else ""
    pane = ""
    if freeze_rows or freeze_columns:
        top_left = f"{_col(freeze_columns + 1)}{freeze_rows + 1}"
        active_pane = (
            "bottomRight" if freeze_rows and freeze_columns
            else "bottomLeft" if freeze_rows else "topRight"
        )
        splits = ""
        if freeze_columns:
            splits += f' xSplit="{freeze_columns}"'
        if freeze_rows:
            splits += f' ySplit="{freeze_rows}"'
        pane = (
            f'<pane{splits} topLeftCell="{top_left}" '
            f'activePane="{active_pane}" state="frozen"/>'
        )
    sheet_views = (
        f'<sheetViews><sheetView workbookViewId="0"{show_gridlines}>'
        f'{pane}</sheetView></sheetViews>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{sheet_views}<cols>{cols}</cols><sheetData>{''.join(rows_xml)}</sheetData>"
        f"{auto_filter}{merge_cells}"
        "</worksheet>"
    )



def _sheet_drawing_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
        'Target="../drawings/drawing1.xml"/>'
        '</Relationships>'
    )


def _drawing_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>'
        '</Relationships>'
    )


def _drawing_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'{_chart_anchor("rId1", 0, 1, 9, 19, "垂域样本分布")}'
        f'{_chart_anchor("rId2", 10, 1, 23, 19, "维度问题占比")}'
        '</xdr:wsDr>'
    )


def _chart_anchor(rid: str, col1: int, row1: int, col2: int, row2: int, name: str) -> str:
    return (
        '<xdr:twoCellAnchor>'
        f'<xdr:from><xdr:col>{col1}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
        f'<xdr:to><xdr:col>{col2}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row2}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
        '<xdr:graphicFrame macro="">'
        '<xdr:nvGraphicFramePr>'
        f'<xdr:cNvPr id="{1 if rid == "rId1" else 2}" name="{escape(name)}"/>'
        '<xdr:cNvGraphicFramePr/>'
        '</xdr:nvGraphicFramePr>'
        '<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="{rid}"/>'
        '</a:graphicData></a:graphic>'
        '</xdr:graphicFrame>'
        '<xdr:clientData/>'
        '</xdr:twoCellAnchor>'
    )


def _quoted_sheet(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _doughnut_chart_xml(data_sheet: str, title: str) -> str:
    sh = _quoted_sheet(data_sheet)
    cats = f"{sh}!$A$2:$A$500"
    vals = f"{sh}!$B$2:$B$500"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<c:chart>'
        f'{_chart_title(title)}'
        '<c:plotArea><c:layout/><c:doughnutChart><c:varyColors val="1"/>'
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f'<c:cat><c:strRef><c:f>{escape(cats)}</c:f></c:strRef></c:cat>'
        f'<c:val><c:numRef><c:f>{escape(vals)}</c:f></c:numRef></c:val>'
        '</c:ser><c:firstSliceAng val="270"/><c:holeSize val="55"/></c:doughnutChart></c:plotArea>'
        '<c:legend><c:legendPos val="r"/><c:layout/></c:legend><c:plotVisOnly val="1"/>'
        '</c:chart></c:chartSpace>'
    )


def _bar_chart_xml(data_sheet: str, title: str) -> str:
    sh = _quoted_sheet(data_sheet)
    cats = f"{sh}!$C$2:$C$500"
    vals = f"{sh}!$D$2:$D$500"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<c:chart>'
        f'{_chart_title(title)}'
        '<c:plotArea><c:layout/><c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>'
        '<c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>问题占比</c:v></c:tx>'
        f'<c:cat><c:strRef><c:f>{escape(cats)}</c:f></c:strRef></c:cat>'
        f'<c:val><c:numRef><c:f>{escape(vals)}</c:f></c:numRef></c:val>'
        '</c:ser><c:axId val="123456"/><c:axId val="123457"/></c:barChart>'
        '<c:catAx><c:axId val="123456"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:axPos val="b"/><c:tickLblPos val="nextTo"/><c:crossAx val="123457"/><c:crosses val="autoZero"/></c:catAx>'
        '<c:valAx><c:axId val="123457"/><c:scaling><c:orientation val="minMax"/><c:max val="1"/><c:min val="0"/></c:scaling>'
        '<c:axPos val="l"/><c:numFmt formatCode="0%" sourceLinked="0"/><c:majorGridlines/><c:tickLblPos val="nextTo"/>'
        '<c:crossAx val="123456"/><c:crosses val="autoZero"/></c:valAx>'
        '</c:plotArea><c:legend><c:legendPos val="b"/><c:layout/></c:legend><c:plotVisOnly val="1"/>'
        '</c:chart></c:chartSpace>'
    )


def _chart_title(title: str) -> str:
    return (
        '<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN"/>'
        f'<a:t>{escape(title)}</a:t>'
        '</a:r></a:p></c:rich></c:tx><c:layout/></c:title>'
    )


def _col(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _width(header: str) -> int:
    if header in {"query", "answer", "generated_answer", "rationale", "理由", "options"}:
        return 42
    if header.startswith("理由_"):
        return 30
    if header.startswith("维度_"):
        return 14
    return 18
