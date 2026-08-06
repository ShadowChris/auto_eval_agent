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

from ..judges.operation_fields import map_legacy_operation_result
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
        "updated_at": time.time(),
        "done_total": task.done_total,
        "error": task.error,
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
        if status in {"pending", "running"}:
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
            "status": status,
            "total": len(data.get("items") or []),
            "done": len([r for r in (data.get("results") or []) if "error" not in r]),
            "created_at": created_at,
            "updated_at": data.get("updated_at") or data.get("created_at"),
            "error": error,
            "preview": _preview(data),
        })
    rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return rows[:limit] if limit > 0 else rows

def _preview(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return ""
    q = str(items[0].get("query") or "")
    return q[:80] + ("…" if len(q) > 80 else "")


def snapshot_payload(data: dict) -> dict:
    data = _with_operation_compat(data)
    return {
        "task_id": data.get("task_id"),
        "session_name": data.get("session_name"),
        "dataset_name": data.get("dataset_name") or "",
        "note": data.get("note") or "",
        "mode": data.get("mode"),
        "items": data.get("items") or [],
        "options": data.get("options") or {},
        "status": data.get("status"),
        "results": data.get("results") or [],
        "item_progress": data.get("item_progress") or {},
        "progress_events": data.get("progress_events") or {},
        "summary": data.get("summary") or {},
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "done_total": int(data.get("done_total") or len(data.get("results") or [])),
        "error": data.get("error"),
    }


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

    rows: dict[str, list[dict]] = {
        "数据集明细": _dataset_rows(
            snapshot,
            compact_media=mode == "operation",
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
    if mode == "operation":
        # 任务类只有一个固定垂域，不再生成重复的按垂域拆分表、
        # 失败表和通用垂域统计表。失败与告警仍在“逐题结果”原行展示。
        rows["运行汇总"] = [_operation_run_summary(snapshot)]
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
    "query",
    "context",
    "answer",
    "task_type",
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
    "评估状态",
    "error",
    "video_prepare_warnings",
)


def _operation_export_rows(results: list[dict], items: list[dict]) -> list[dict]:
    """将任务类结果转为固定列。

    原始数据集字段由“数据集明细”完整保留；这里只展示任务类的
    核心评估字段。结果缺失时从原始 item 回填输入字段，评分字段留空。
    """
    export: list[dict] = []
    for position, result in enumerate(results):
        item = items[position] if position < len(items) else {}
        rubric = result.get("rubric") or {}
        reasons = result.get("rubric_reasons") or {}
        values = {
            "数据集序号": result.get("数据集序号", position + 1),
            "item_id": result.get("item_id") or item.get("id") or f"q{position}",
            "query": result.get("query") or item.get("query") or item.get("question") or "",
            "context": result.get("context") or item.get("context") or "",
            "answer": result.get("answer") or item.get("answer") or "",
            "task_type": result.get("task_type", ""),
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
            "评估状态": result.get("评估状态", ""),
            "error": result.get("error", ""),
            "video_prepare_warnings": result.get("video_prepare_warnings", ""),
        }
        for key in ("issue_types", "video_prepare_warnings"):
            if isinstance(values[key], list):
                values[key] = "；".join(str(value) for value in values[key])
        export.append({key: values[key] for key in _OPERATION_EXPORT_COLUMNS})
    return export

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
    for item_index, item in enumerate(snapshot.get("items") or []):
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
        "options": snapshot.get("options") or {},
        "error": snapshot.get("error") or "",
    }


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
        "judges": judges,
        "model": options.get("model") or "",
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
    items = snapshot.get("items") or []
    width = max(3, len(str(max(len(items), 1))))

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item_index, item in enumerate(items):
            if item_indexes is not None and item_index not in item_indexes:
                continue
            sequence = str(item_index + 1).zfill(width)
            raw_id = str(item.get("id") or f"q{item_index + 1}")
            safe_id = _safe_name(raw_id).strip("_")[:100] or f"q{item_index + 1}"
            item_dir = f"{sequence}_{safe_id}"
            frames = [Path(str(path)) for path in (item.get("frames") or [])]
            selected, metadata = (
                _frame_metadata(frames[0].parent) if frames else ({}, {})
            )
            source = _source_data_for_item(item)
            source_video = source.get("video_path") or ""
            if not frames:
                manifest.append({
                    "dataset_index": item_index + 1,
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
                    "dataset_index": item_index + 1,
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
    错配。候选日志包含当前环境配置的 trace 路径和 runs 下所有
    ``judge_calls*.jsonl``，因此加载历史任务后仍可导出。
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
        configured = str(os.getenv("AUTO_EVAL_JUDGE_TRACE") or "").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if not configured_path.is_absolute():
                configured_path = project_root / configured_path
            candidates.append(configured_path)
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
        for i, (_name, rows) in enumerate(sheets.items(), start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
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
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )


def _sheet_xml(rows: list[dict]) -> str:
    headers = _headers(rows)
    table = [headers] + [[row.get(h) for h in headers] for row in rows]
    rows_xml = []
    for r_idx, row in enumerate(table, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = f"{_col(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            if (
                r_idx > 1
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            ):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(_cell(value))}</t></is></c>')
        rows_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{_width(h)}" customWidth="1"/>'
        for i, h in enumerate(headers, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{cols}</cols><sheetData>{''.join(rows_xml)}</sheetData>"
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
