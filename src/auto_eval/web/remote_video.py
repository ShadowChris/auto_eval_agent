"""任务类远程视频临时下载，与抽帧算法、结果预览独立。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
import ipaddress
import math
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml

from ..paths import PROJECT_ROOT, RUNS_DIR
from ..observability import log_event
from .pipeline_config import current_pipeline_config as load_pipeline_config


CONFIG_PATH = PROJECT_ROOT / "config" / "remote_video.yaml"
_CANCEL: ContextVar[threading.Event | None] = ContextVar("remote_video_cancel", default=None)
_PREPARED_INPUT: ContextVar[tuple[str, Path] | None] = ContextVar("prepared_video_input", default=None)
_DOWNLOAD_SLOT: ContextVar[bool] = ContextVar("video_download_slot", default=False)
_LOCK = threading.Lock()
_LIMITERS: dict[int, threading.BoundedSemaphore] = {}
_ACTIVE: set[Path] = set()


@dataclass(frozen=True)
class RemoteVideoConfig:
    download_concurrency: int = 10
    connect_timeout_s: float = 10
    read_timeout_s: float = 30
    total_timeout_s: float = 120
    max_size_mb: float = 500
    max_redirects: int = 5
    max_attempts: int = 3
    retry_delay_s: float = 1
    stale_after_hours: float = 24
    allowed_hosts: tuple[str, ...] = ()


def load_remote_video_config() -> RemoteVideoConfig:
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    data = data or {}
    known = {field.name for field in fields(RemoteVideoConfig)}
    if not isinstance(data, dict) or set(data) - known:
        raise ValueError("remote_video.yaml 配置字段无效")
    data.setdefault("download_concurrency", load_pipeline_config().download_concurrency)
    cfg = RemoteVideoConfig(**data)
    for name in known - {"allowed_hosts"}:
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"远程视频配置 {name} 必须是有限数字")
        if value < 0 or (name not in {"max_redirects", "retry_delay_s"} and value == 0):
            raise ValueError(f"远程视频配置 {name} 超出有效范围")
    for name in ("download_concurrency", "max_redirects", "max_attempts"):
        if not isinstance(getattr(cfg, name), int):
            raise ValueError(f"远程视频配置 {name} 必须是整数")
    if not isinstance(cfg.allowed_hosts, (list, tuple)) or any(
        not isinstance(host, str) or not host.strip() or "/" in host or "*" in host
        for host in cfg.allowed_hosts
    ):
        raise ValueError("allowed_hosts 必须是主机名列表")
    return cfg


def is_remote_video_url(value: str) -> bool:
    return str(value).strip().lower().startswith(("http://", "https://"))


def video_reference_for_log(value: str) -> str:
    """日志去掉签名查询参数；原始输入和导出仍完整保留。"""
    if not is_remote_video_url(value):
        return value
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    except ValueError:
        return "<无效视频 URL>"


def _checked_addresses(url: str, cfg: RemoteVideoConfig) -> tuple[httpx.URL, list[str]]:
    try:
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
            raise ValueError("只支持不含用户名密码的 HTTP/HTTPS 视频链接")
        addresses = list(dict.fromkeys(
            row[4][0] for row in socket.getaddrinfo(
                parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        ))
        allowed = parsed.host.lower() in {host.lower() for host in cfg.allowed_hosts}
        if not addresses or (not allowed and any(not ipaddress.ip_address(ip).is_global for ip in addresses)):
            raise ValueError("视频链接指向非公网地址；内网 OBS 需配置 allowed_hosts")
        return parsed, addresses
    except (httpx.InvalidURL, socket.gaierror) as exc:
        raise ValueError("视频 URL 无效或域名无法解析") from exc


def _check_stop(cancel: threading.Event | None, deadline: float) -> None:
    if cancel is not None and cancel.is_set():
        raise ValueError("远程视频准备已取消")
    if time.monotonic() >= deadline:
        raise ValueError("远程视频下载超过总时间限制")


def download_remote_video(
    url: str, directory: Path, *, config: RemoteVideoConfig | None = None,
) -> Path:
    """下载到调用方拥有的临时目录；失败不留下半文件，不返回原始签名错误。"""
    cfg = config or load_remote_video_config()
    directory.mkdir(parents=True, exist_ok=True)
    cancel = _CANCEL.get()
    with _LOCK:
        limiter = _LIMITERS.setdefault(cfg.download_concurrency, threading.BoundedSemaphore(cfg.download_concurrency))
    owns_slot = not _DOWNLOAD_SLOT.get()
    while owns_slot and not limiter.acquire(timeout=0.2):
        if cancel is not None and cancel.is_set():
            raise ValueError("远程视频准备已取消")
    part = directory / "video.part"
    target = directory / "video.mp4"
    deadline = time.monotonic() + cfg.total_timeout_s
    max_bytes = int(cfg.max_size_mb * 1024 * 1024)
    try:
        for attempt in range(cfg.max_attempts):
            _check_stop(cancel, deadline)
            current_url = url.strip()
            try:
                # 禁用环境代理，确保连接到已经校验的 IP；HTTPS 保留原域名用于证书校验。
                with httpx.Client(trust_env=False, follow_redirects=False) as client:
                    for redirect in range(cfg.max_redirects + 1):
                        parsed, addresses = _checked_addresses(current_url, cfg)
                        remaining = max(0.1, deadline - time.monotonic())
                        pinned = parsed.copy_with(host=addresses[attempt % len(addresses)])
                        request = client.build_request(
                            "GET", pinned,
                            headers={"Host": parsed.netloc.decode("ascii"), "Accept-Encoding": "identity"},
                            extensions={"sni_hostname": parsed.host},
                            timeout=httpx.Timeout(
                                min(cfg.read_timeout_s, remaining),
                                connect=min(cfg.connect_timeout_s, remaining),
                            ),
                        )
                        response = client.send(request, stream=True)
                        try:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                if redirect == cfg.max_redirects or not response.headers.get("location"):
                                    raise ValueError("远程视频重定向过多或缺少目标地址")
                                current_url = urljoin(current_url, response.headers["location"])
                                continue
                            response.raise_for_status()
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                            if content_type in {"text/html", "application/json", "text/plain"}:
                                raise ValueError("链接返回的是网页或错误信息，不是视频文件")
                            length = response.headers.get("content-length", "")
                            if length.isdigit() and int(length) > max_bytes:
                                raise ValueError(f"远程视频超过 {cfg.max_size_mb:g}MB 大小限制")
                            size = 0
                            with part.open("wb") as handle:
                                for chunk in response.iter_bytes():
                                    _check_stop(cancel, deadline)
                                    size += len(chunk)
                                    if size > max_bytes:
                                        raise ValueError(f"远程视频超过 {cfg.max_size_mb:g}MB 大小限制")
                                    handle.write(chunk)
                            _check_stop(cancel, deadline)
                            if not size:
                                raise ValueError("远程视频为空文件")
                            part.replace(target)
                            return target
                        finally:
                            response.close()
            except (httpx.HTTPError, OSError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable = status is None or status in {408, 429, 500, 502, 503, 504}
                if attempt + 1 >= cfg.max_attempts or not retryable:
                    reason = f"HTTP {status}" if status is not None else type(exc).__name__
                    raise ValueError(f"远程视频下载失败：{reason}（{video_reference_for_log(url)}）") from None
                delay = min(cfg.retry_delay_s * (2 ** attempt), max(0, deadline - time.monotonic()))
                if cancel is not None:
                    cancel.wait(delay)
                else:
                    time.sleep(delay)
            finally:
                part.unlink(missing_ok=True)
        raise ValueError("远程视频下载失败")
    finally:
        if owns_slot:
            limiter.release()


@contextmanager
def local_video_input(raw: str, runs_dir: Path) -> Iterator[Path | None]:
    """本地输入不处理；远程输入只在本次准备期间持有临时原视频。"""
    prepared = _PREPARED_INPUT.get()
    if prepared is not None and prepared[0] == raw:
        yield prepared[1]
        return
    if not is_remote_video_url(raw):
        yield None
        return
    root = runs_dir / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video_", dir=root) as temp:
        directory = Path(temp)
        (directory / ".operation_video_download").touch()
        with _LOCK:
            _ACTIVE.add(directory.resolve())
        try:
            log_event(
                "视频准备", "下载远程视频", details={"视频链接": video_reference_for_log(raw)},
                progress=4, progress_message="正在下载远程视频",
            )
            yield download_remote_video(raw, directory)
        finally:
            with _LOCK:
                _ACTIVE.discard(directory.resolve())


async def run_video_preparation(function: Callable, *args, **kwargs):
    """兼容准备入口；内部委托阶段调度，不改变准备函数返回值。"""
    from .video_pipeline import prepare_video

    return await prepare_video(function, *args, **kwargs)


def cleanup_stale_video_downloads(runs_dir: Path = RUNS_DIR) -> int:
    """启动时只回收本模块创建的过期临时目录，不清理用户媒体和抽帧。"""
    cfg = load_remote_video_config()
    cutoff = time.time() - cfg.stale_after_hours * 3600
    removed = 0
    for directory in (runs_dir / "downloads").glob("video_*"):
        try:
            with _LOCK:
                if directory.resolve() in _ACTIVE:
                    continue
            marker = directory / ".operation_video_download"
            if (
                directory.is_symlink() or not directory.is_dir()
                or marker.is_symlink() or not marker.is_file()
                or directory.stat().st_mtime >= cutoff
            ):
                continue
            shutil.rmtree(directory)
            removed += 1
        except OSError:
            # 残留清理不能阻止服务启动；目录可能被另一进程清理或暂时被占用。
            continue
    return removed
