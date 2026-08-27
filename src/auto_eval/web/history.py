"""Web 评测历史持久化与完整导出。

这里刻意不用数据库：评测台是本地/轻量服务，JSON 快照足够支撑历史加载；
XLSX 直接生成 OOXML，避免给项目额外引入 openpyxl / xlsxwriter 依赖。
"""
from __future__ import annotations

import json
import hashlib
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

from ..paths import PROJECT_ROOT, RUNS_DIR


HISTORY_DIR = RUNS_DIR / "web_history"
logger = logging.getLogger(__name__)


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "_", value)


def _task_id_slug(task_id: str) -> str:
    """task_id 的文件名安全形态；净化有损（如中文/符号 id）时追加短哈希。

    不追加哈希的话，不同原始 id 净化后同名（如同长度中文 id），同一秒创建
    的两个任务会生成相同 session_name、快照文件互相覆盖。
    """
    safe = _safe_name(task_id)
    if safe != task_id:
        digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe}-{digest}"
    return safe


def make_session_name(created_at: float, mode: str, task_id: str) -> str:
    """生成可按文件名排序、同时能关联任务的稳定会话名。"""
    dt = datetime.fromtimestamp(created_at).astimezone()
    return f"{dt:%Y%m%d_%H%M%S}_{_safe_name(mode)}_{_task_id_slug(task_id)}"


def _snapshot_task_id_matches(path: Path, task_id: str) -> bool:
    """校验快照 JSON 内的 task_id 与请求的原始 id 一致。

    _safe_name 会把非 [0-9a-zA-Z_-] 字符统一替换成 "_"：放开 task_id 字符集后，
    不同原始 id 可能净化成同一文件名模式（如同长度中文 id）。glob/旧文件名命中
    后必须读字段校验，只认真正属于该 id 的快照；旧快照缺 task_id 字段时保留
    原命中以兼容。
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    stored = data.get("task_id")
    return stored is None or stored == task_id


def _find_task_path(task_id: str) -> Path:
    """优先查旧文件名，再查带时间前缀的新文件名。

    新文件名用 _task_id_slug（有损 id 带短哈希），命中后均校验快照内的
    task_id 字段（见 _snapshot_task_id_matches）；全部不一致时返回不存在
    的旧路径，调用方按"任务不存在"处理。
    """
    legacy = HISTORY_DIR / f"{_safe_name(task_id)}.json"
    if legacy.exists() and _snapshot_task_id_matches(legacy, task_id):
        return legacy
    matches = sorted(HISTORY_DIR.glob(f"*_{_task_id_slug(task_id)}.json"))
    verified = [p for p in matches if _snapshot_task_id_matches(p, task_id)]
    return verified[-1] if verified else legacy


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
    snapshot = task_to_snapshot(task)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            _write_meta(path, _snapshot_meta_row(snapshot, path))
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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_snapshot(task_id: str) -> bool:
    """删除某次评测的快照文件（连同摘要侧车）。返回是否删除成功。"""
    path = _find_task_path(task_id)
    if not path.exists():
        return False
    try:
        path.unlink()
    except Exception:
        return False
    try:
        _meta_path(path).unlink(missing_ok=True)
    except Exception:
        pass
    return True


_META_SUFFIX = ".meta.json"


def _meta_path(path: Path) -> Path:
    """快照 X.json 的摘要侧车路径（X.json.meta.json）。

    侧车与主快照同名前缀，列表页只读它（几百字节）而不必把每个历史快照
    全量 read_text + json.loads——快照随使用时间累积后那是数百 MB 级的
    瞬时内存分配。侧车名以 ".meta.json" 结尾，_find_task_path 的
    "*_{slug}.json" glob 不会误命中（结尾是 "." 不是 "_"）。
    """
    return path.with_name(path.name + _META_SUFFIX)


def _apply_interrupted_status(status, error):
    """盘上停在 pending/running 只可能是服务中断，列表统一改写为 error。"""
    if status in {"pending", "running"}:
        return "error", error or "服务中断，已保留中断前完成的评估结果"
    return status, error


def _snapshot_meta_row(data: dict, path: Path) -> dict:
    """从完整快照 dict 计算历史列表行（摘要字段），存原始 status/error。"""
    task_id = data.get("task_id") or path.stem
    created_at = data.get("created_at")
    session_name = data.get("session_name") or (
        path.stem
        if path.stem != _safe_name(str(task_id))
        else make_session_name(float(created_at or 0), data.get("mode") or "unknown", str(task_id))
    )
    return {
        "task_id": task_id,
        "session_name": session_name,
        "dataset_name": data.get("dataset_name") or "",
        "note": data.get("note") or "",
        "mode": data.get("mode"),
        "status": data.get("status"),
        "total": len(data.get("items") or []),
        "done": len([r for r in (data.get("results") or []) if "error" not in r]),
        "created_at": created_at,
        "updated_at": data.get("updated_at") or data.get("created_at"),
        "error": data.get("error"),
        "preview": _preview(data),
        "meta_version": 1,
    }


def _write_meta(snapshot_path: Path, row: dict) -> None:
    """原子写摘要侧车；best-effort，失败仅告警不影响主快照。"""
    meta_path = _meta_path(snapshot_path)
    tmp = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, meta_path)
    except OSError as exc:
        logger.warning(
            "历史摘要侧车写入失败: %s error=%s", meta_path.name, exc
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_meta_row(path: Path) -> dict | None:
    """读单个快照的列表行：优先侧车；miss/损坏则全量读主快照一次并补写
    侧车（legacy 自愈）；两者都不可读返回 None（跳过该条）。"""
    row: dict | None = None
    meta_path = _meta_path(path)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                row = data
        except Exception:
            row = None
    if row is None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        row = _snapshot_meta_row(data, path)
        _write_meta(path, row)
    row.pop("meta_version", None)  # 版本号只落侧车，响应形状与旧实现一致
    status, error = _apply_interrupted_status(row.get("status"), row.get("error"))
    row["status"] = status
    row["error"] = error
    return row


def list_snapshots(limit: int = 50) -> list[dict]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in HISTORY_DIR.glob("*.json"):
        if path.name.endswith(_META_SUFFIX):
            continue
        row = _load_meta_row(path)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return rows[:limit]

def _preview(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return ""
    q = str(items[0].get("query") or "")
    return q[:80] + ("…" if len(q) > 80 else "")


def snapshot_payload(data: dict) -> dict:
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
        "error": data.get("error"),
    }


def export_rows(snapshot: dict) -> dict[str, list[dict]]:
    """把一次评测拆成多个 Sheet 的行数据。

    ``数据集明细`` 与 ``逐题结果`` 都以原始 items 为主表，严格按输入顺序
    一一对齐。并发评测导致的完成顺序变化不会影响导出；失败或待评估条目
    仍占据原行，只将评分字段留空。

    rich_content 与 compare 均按对外定名列导出（同时供单条结果查询复用）；
    其余（已删模式的旧历史快照）按维度展开成独立列（维度_X / 理由_X）兜底。
    """
    results = _results_with_identity(snapshot)
    aligned_results = _aligned_results(snapshot, results)
    summary = snapshot.get("summary") or {}
    mode = snapshot.get("mode")

    if mode == "rich_content":
        result_rows = _rich_content_export_rows(aligned_results)
    elif mode == "compare":
        result_rows = _visual_compare_export_rows(aligned_results)
    else:
        result_rows = _result_rows(aligned_results)
    rows: dict[str, list[dict]] = {
        "数据集明细": _dataset_rows(snapshot),
        "逐题结果": result_rows,
    }
    frame_rows = _frame_manifest_rows(snapshot)
    if frame_rows:
        rows["抽帧清单"] = frame_rows
    rows["运行信息"] = [_run_info(snapshot)]
    if summary:
        rows["汇总指标"] = [_flatten_dict(summary, skip_keys={"by_category"})]
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

# 垂域视觉评测（rich_content）Excel/CSV 导出列：按此顺序输出。
# query_id / 垂域分类 / 卡片存在情况 / correctness / error_type 为外部对接定名；
# Superlink 合适度、回答覆盖、耗时不再导出。
_RICH_CONTENT_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("item_id", "query_id"),
    ("query", "Query"),
    ("context", "context"),
    ("category_display", "垂域分类"),
    ("answer_text", "answer_text"),
    ("card_presence_label", "卡片存在情况"),
    ("card_count", "卡片数量"),
    ("card_types", "卡片种类"),
    ("card_contents", "卡片内容"),
    ("superlink_presence_label", "Superlink存在情况"),
    ("superlink_count", "Superlink数量"),
    ("superlink_texts", "Superlink文字"),
    ("card_suitability", "卡片是否合适"),
    ("card_suitability_reason", "卡片不合适原因"),
    ("needs_review_label", "识别是否需要人工复查"),
    ("review_reason", "需要复核的原因"),
    ("problem_solved", "correctness"),
    ("problem_solved_reason", "评价的原因"),
    ("answer_issues", "error_type"),
    ("rationale", "识别结论"),
    ("analysis", "评价分析过程"),
]

# 列表类字段取值后需要拼接为字符串
_RICH_CONTENT_LIST_FIELDS = {"card_types", "card_contents", "superlink_texts"}

# 枚举值 → 展示值映射
_RICH_CONTENT_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "card_suitability": {"ok": "OK", "nok": "NOK"},
    "problem_solved": {"ok": "OK", "nok": "NOK", "need_review": "需复查"},
}


def _rich_content_export_rows(results: list[dict]) -> list[dict]:
    """将 rich_content 结果行按导出列顺序重排并转换为对外定名（见列定义注释）。"""
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


def _dataset_rows(snapshot: dict) -> list[dict]:
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
        frame_project_paths = [
            path for path in (
                _project_relative_path(frame)
                for frame in frames
            )
            if path
        ]
        frame_dir = (
            _project_relative_path(Path(frames[0]).parent)
            if frames else ""
        )
        row.update({
            "录屏项目相对路径": _project_relative_path(video_runtime_path),
            "抽帧目录项目相对路径": frame_dir,
            "帧项目相对路径": "\n".join(frame_project_paths),
            "抽帧数量": item.get("frame_count") or len(frames),
            "录屏时长（秒）": item.get("duration") or "",
        })
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


def _frame_manifest_rows(snapshot: dict) -> list[dict]:
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
        source = _source_data_for_item(item)
        source_video = source.get("video_path") or item.get("video_path") or ""
        base = {
            "数据集序号": item_index + 1,
            "id": item.get("id") or f"q{item_index}",
            "query": item.get("query") or item.get("question") or "",
            "录屏项目相对路径": _project_relative_path(item.get("video_path")),
            "原始video_path": source_video,
        }
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


def _format_ts(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")

def _visual_compare_export_rows(results: list[dict]) -> list[dict]:
    """垂域视觉对比导出：维度对比结论 + 内容冲突。"""
    _COMPARE_COLUMNS: list[tuple[str, str]] = [
        ("item_id", "query_id"),
        ("query", "题目"),
        ("context", "背景"),
        ("context1", "产品1背景"),
        ("answer1", "产品1回答"),
        ("context2", "产品2背景"),
        ("answer2", "产品2回答"),
        ("relevance", "相关性"),
        ("safety", "安全合规"),
        ("content_quality", "内容质量"),
        ("need_closure", "需求闭环"),
        ("personalization", "个性化一致性"),
        ("has_conflict", "内容冲突"),
        ("rationale", "理由"),
    ]
    _DISPLAY_MAP = {
        "relevance": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "safety": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "content_quality": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "need_closure": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "personalization": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "has_conflict": {"yes": "有冲突", "no": "无冲突", "unclear": "不清楚"},
    }
    rows = []
    for r in results:
        row = {}
        for key, label in _COMPARE_COLUMNS:
            v = r.get(key)
            if v is None:
                row[label] = "N/A"
            elif key in _DISPLAY_MAP and v in _DISPLAY_MAP[key]:
                row[label] = _DISPLAY_MAP[key][v]
            else:
                row[label] = v if v != "" else ""
        rows.append(row)
    return rows


def result_export_row(mode: str, result: dict, index: int, items: list[dict]) -> dict:
    """单条 result → 与 xlsx/CSV「逐题结果」同名列、同转换的行。

    供 GET /api/eval/item/result 使用：键名与导出列完全一致，
    多余字段不返回；失败结果额外附加 error。item_id/query 缺失时
    按 index 从 items 回填，与导出的对齐逻辑保持一致。
    """
    row = dict(result)
    item = items[index] if 0 <= index < len(items) else {}
    if not row.get("item_id"):
        row["item_id"] = item.get("id") or f"q{index}"
    if not row.get("query"):
        row["query"] = item.get("query") or item.get("question") or ""
    export = (
        _visual_compare_export_rows([row])
        if mode == "compare"
        else _rich_content_export_rows([row])
    )[0]
    if result.get("error"):
        export["error"] = result["error"]
    return export


def _result_rows(results: list[dict]) -> list[dict]:
    """旧模式快照兜底：把维度展开成 分(维度_X) / 理由(理由_X) 两列。"""
    rows = []
    for r in results:
        row = dict(r)
        rubric = row.pop("rubric", {}) or {}
        reasons = row.pop("rubric_reasons", {}) or {}
        for dim, score in rubric.items():
            row[f"维度_{dim}"] = score
            row[f"理由_{dim}"] = reasons.get(dim, "")
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


def build_xlsx(snapshot: dict) -> bytes:
    """生成 xlsx（纯数据 sheet，不含图表）。

    手写 OOXML chart 易被 Excel 判"需修复"且样式差，故只导出数据；
    如需图表，用导出的汇总数据在 Excel 中自行插入。
    """
    sheets = {name: rows for name, rows in export_rows(snapshot).items() if rows}
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
