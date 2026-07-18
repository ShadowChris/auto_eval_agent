"""低耦合实时调用链日志。

完整模型审计仍由 ``judge_calls.jsonl`` 承担；本模块只记录便于实时排障的中文摘要，
并把同一事件投影为 Web 逐题进度。ContextVar 保证 asyncio 并发链路互不串线。
"""
from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import queue
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any, Callable, Iterator

from .paths import PROJECT_ROOT


@dataclass(frozen=True)
class ChainContext:
    task_id: str = "-"
    session_name: str = "-"
    request_id: str = "-"
    item_id: str = "-"
    item_index: int = -1
    module: str = ""
    judge: str = ""
    round: int = 0
    progress_callback: Callable[[dict], None] | None = None


_context: ContextVar[ChainContext] = ContextVar(
    "auto_eval_chain_context", default=ChainContext()
)
_logger: logging.Logger | None = None
_listener: QueueListener | None = None
_setup_lock = threading.Lock()
_auto_id_counter = itertools.count(1)


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _auto_request_id() -> str:
    now = datetime.now().astimezone()
    return f"{now:%y%m%d%H%M}_auto_{next(_auto_id_counter):04d}"


def current_context() -> ChainContext:
    value = _context.get()
    # Web 入口会绑定正式 req_id；CLI、脚本或以后新增的独立入口也不能留下 [-]。
    # pytest 默认禁用正式文件日志，因此测试里保留 "-" 便于断言上下文恢复。
    if value.request_id == "-" and not _running_under_pytest():
        value = replace(value, request_id=_auto_request_id())
        _context.set(value)
    return value


@contextmanager
def bind_chain_context(**changes) -> Iterator[ChainContext]:
    """在当前异步调用链绑定字段，退出后自动恢复父级上下文。"""
    value = replace(current_context(), **changes)
    token = _context.set(value)
    try:
        yield value
    finally:
        _context.reset(token)


def make_request_id(created_at: float, task_id: str, item_index: int) -> str:
    dt = datetime.fromtimestamp(created_at).astimezone()
    return f"{dt:%y%m%d%H%M}_{task_id[:6]}_q{item_index}"


class _DailySizeHandler(logging.Handler):
    """按本地日期和大小滚动；不删除任何历史文件。"""

    terminator = "\n"

    def __init__(self, log_dir: Path, max_bytes: int):
        super().__init__()
        self.log_dir = log_dir
        self.max_bytes = max(1, max_bytes)
        self._date = ""
        self._index = 0
        self._path: Path | None = None
        self._stream = None

    def _candidate(self, date: str, index: int) -> Path:
        suffix = "" if index == 0 else f".{index:03d}"
        return self.log_dir / f"{date}{suffix}.log"

    def _select_path(self, date: str, incoming: int) -> None:
        if date != self._date:
            self._close_stream()
            self._date = date
            self._index = 0
            self.log_dir.mkdir(parents=True, exist_ok=True)
            while True:
                candidate = self._candidate(date, self._index)
                if not candidate.exists() or candidate.stat().st_size < self.max_bytes:
                    self._path = candidate
                    break
                self._index += 1
        assert self._path is not None
        size = self._path.stat().st_size if self._path.exists() else 0
        if size and size + incoming > self.max_bytes:
            self._close_stream()
            self._index += 1
            self._path = self._candidate(date, self._index)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record) + self.terminator
            encoded = text.encode("utf-8")
            date = datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%d")
            self._select_path(date, len(encoded))
            if self._stream is None:
                assert self._path is not None
                self._stream = self._path.open("a", encoding="utf-8", newline="")
            self._stream.write(text)
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        self._close_stream()
        super().close()


class _ChineseFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created).astimezone()
        millis = dt.microsecond // 1000
        ts = f"{dt:%Y-%m-%d %H:%M:%S}.{millis:03d}"
        request_id = getattr(record, "request_id", "-")
        module = getattr(record, "chain_module", "系统")
        round_no = int(getattr(record, "round_no", 0) or 0)
        round_text = f"[第{round_no}轮]" if round_no > 0 else ""
        event = getattr(record, "chain_event", record.getMessage())
        details = getattr(record, "chain_details", {})
        detail_text = _format_details(details)
        level = "WARN" if record.levelname == "WARNING" else record.levelname
        line = (
            f"{ts} {level:<5} [{request_id}] "
            f"[{module}]{round_text} {event}"
        )
        if detail_text:
            line += f" | {detail_text}"
        return line


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "-"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if any(ch.isspace() for ch in text) or "|" in text or "=" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _format_details(details: dict[str, Any]) -> str:
    return " | ".join(
        f"{key}={_display(value)}"
        for key, value in details.items()
        if value is not None and value != ""
    )


def _ensure_logger() -> logging.Logger:
    global _logger, _listener
    if _logger is not None:
        return _logger
    with _setup_lock:
        if _logger is not None:
            return _logger
        logger = logging.getLogger("auto_eval.chain")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()
        enabled = os.environ.get("AUTO_EVAL_CHAIN_LOG_ENABLED", "true").lower() not in {
            "0", "false", "no", "off"
        }
        log_tests = os.environ.get("AUTO_EVAL_CHAIN_LOG_TESTS", "false").lower() in {
            "1", "true", "yes", "on"
        }
        if enabled and (not _running_under_pytest() or log_tests):
            log_dir = Path(os.environ.get("AUTO_EVAL_CHAIN_LOG_DIR", "logs"))
            if not log_dir.is_absolute():
                log_dir = PROJECT_ROOT / log_dir
            max_bytes = int(
                os.environ.get("AUTO_EVAL_CHAIN_LOG_MAX_BYTES", str(50 * 1024 * 1024))
            )
            handler = _DailySizeHandler(log_dir, max_bytes)
            handler.setFormatter(_ChineseFormatter())
            q: queue.SimpleQueue = queue.SimpleQueue()
            logger.addHandler(QueueHandler(q))
            _listener = QueueListener(q, handler, respect_handler_level=True)
            _listener.start()
        else:
            logger.addHandler(logging.NullHandler())
        _logger = logger
        return logger


def shutdown_chain_logging() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


atexit.register(shutdown_chain_logging)


def log_event(
    module: str,
    event: str,
    *,
    level: int = logging.INFO,
    details: dict[str, Any] | None = None,
    progress: int | None = None,
    progress_message: str | None = None,
    progress_status: str = "running",
    progress_fields: dict[str, Any] | None = None,
) -> None:
    ctx = current_context()
    payload = dict(details or {})
    if ctx.judge and "裁判" not in payload:
        payload["裁判"] = ctx.judge
    _ensure_logger().log(
        level,
        event,
        extra={
            "request_id": ctx.request_id,
            "chain_module": module,
            "round_no": ctx.round,
            "chain_event": event,
            "chain_details": payload,
        },
    )
    if ctx.progress_callback and progress is not None:
        try:
            progress_payload = {
                    "request_id": ctx.request_id,
                    "item_id": ctx.item_id,
                    "item_index": ctx.item_index,
                    "status": progress_status,
                    "percent": max(0, min(100, int(progress))),
                    "message": progress_message or f"{module}：{event}",
                    "module": module,
                    "event": event,
                    "level": logging.getLevelName(level).lower(),
                    "judge": ctx.judge or None,
                    "round": ctx.round,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                }
            if progress_fields:
                progress_payload.update(progress_fields)
            ctx.progress_callback(progress_payload)
        except Exception:
            pass


def error_details(exc: BaseException, *, include_traceback: bool = True) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    provider_request_id = (
        getattr(exc, "request_id", None)
        or headers.get("x-request-id")
        or headers.get("request-id")
    )
    details: dict[str, Any] = {
        "错误类型": type(exc).__name__,
        "错误": str(exc),
        "HTTP状态": getattr(exc, "status_code", None)
        or getattr(response, "status_code", None),
        "服务商请求ID": provider_request_id,
    }
    body = getattr(exc, "body", None)
    if body:
        details["响应"] = body
    if include_traceback:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        if text:
            details["调用栈"] = text
    return details
