"""裁判调用日志的文件路径约定。"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from ..paths import PROJECT_ROOT, RUNS_DIR, resolve_project_path


TRACE_DIR_ENV = "AUTO_EVAL_JUDGE_TRACE_DIR"
LEGACY_TRACE_ENV = "AUTO_EVAL_JUDGE_TRACE"
TRACE_ENABLED_ENV = "AUTO_EVAL_JUDGE_TRACE_ENABLED"
DEFAULT_TRACE_DIR = RUNS_DIR / "judge_calls"
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _safe_task_id(task_id: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", str(task_id).strip()).strip("_")
    return safe or "unknown_task"


def _session_date(session_name: str) -> str:
    """从 Web session 名读取任务创建日期，无法读取时回退到当天。"""
    match = re.match(r"^(\d{8})(?:_|$)", str(session_name or ""))
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def judge_trace_enabled() -> bool:
    raw = str(os.getenv(TRACE_ENABLED_ENV, "true") or "").strip().lower()
    return raw not in _FALSE_VALUES


def configured_trace_dir() -> Path | None:
    if not judge_trace_enabled():
        return None
    raw = str(os.getenv(TRACE_DIR_ENV) or "").strip()
    if raw:
        return resolve_project_path(raw)

    # 无需强制用户立即修改旧 .env：历史默认文件自动升级到新的任务级目录。
    legacy = configured_legacy_trace_path()
    if legacy is not None:
        try:
            if legacy.resolve() == (RUNS_DIR / "judge_calls.jsonl").resolve():
                return DEFAULT_TRACE_DIR
        except OSError:
            pass
        # 其他旧配置保持精确文件语义，不同时启用默认目录。
        return None
    return DEFAULT_TRACE_DIR


def configured_legacy_trace_path() -> Path | None:
    raw = str(os.getenv(LEGACY_TRACE_ENV) or "").strip()
    return resolve_project_path(raw) if raw else None


def task_trace_path(
    trace_dir: str | Path,
    *,
    task_id: str,
    session_name: str,
) -> Path:
    root = resolve_project_path(trace_dir)
    return (
        root
        / _session_date(session_name)
        / _safe_task_id(task_id)
        / "judge_calls.jsonl"
    )


def configured_task_trace_path(task_id: str, session_name: str) -> Path | None:
    trace_dir = configured_trace_dir()
    if trace_dir is None:
        return None
    return task_trace_path(
        trace_dir,
        task_id=task_id,
        session_name=session_name,
    )


def configured_write_trace_path(task_id: str, session_name: str) -> Path | None:
    """返回当前配置实际写入的任务日志；旧自定义配置仍可指向精确文件。"""
    if not judge_trace_enabled():
        return None
    task_path = configured_task_trace_path(task_id, session_name)
    if task_path is not None:
        return task_path
    return configured_legacy_trace_path()


def trace_path_reference(path: Path, project_root: Path = PROJECT_ROOT) -> str:
    """快照优先保存项目相对路径，外部存储目录则保留绝对路径。"""
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except (OSError, ValueError):
        return str(path)
