"""上传评估结果集独立持久化；历史列表只读取轻量摘要，不缓存逐题数据。"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class ComparisonUploadStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    def _directory(self, source_id: str) -> Path:
        if not re.fullmatch(r"upload_[0-9a-f]{12}", source_id):
            raise ValueError("上传结果集 ID 无效")
        return self.root / source_id

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        """同目录临时文件 + replace，避免读到写入了一半的 JSON。"""
        fd, name = tempfile.mkstemp(prefix=".writing_", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, allow_nan=False)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def save(self, source: dict[str, Any]) -> dict[str, Any]:
        directory = self._directory(source["source_id"])
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if directory.exists():
                raise ValueError("上传结果集 ID 已存在，不允许覆盖")
            summary = {
                key: source[key]
                for key in (
                    "source_id", "task_id", "source_type", "dataset_name",
                    "group_name", "mapping", "summary",
                )
            }
            summary.update({
                "stored": True,
                "filename": source["dataset_name"],
                "created_at": time.time(),
                "note": "",
                "warnings": source.get("warnings", [])[:20],
            })
            staging = Path(tempfile.mkdtemp(prefix=".upload_", dir=self.root))
            try:
                self._write_json(staging / "source.json", source)
                self._write_json(staging / "summary.json", summary)
                staging.rename(directory)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return summary

    def summary(self, source_id: str) -> dict[str, Any]:
        path = self._directory(source_id) / "summary.json"
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def load(self, source_id: str) -> dict[str, Any]:
        directory = self._directory(source_id)
        with self._lock:
            summary = self.summary(source_id)
            source = json.loads((directory / "source.json").read_text(encoding="utf-8"))
            # 原始名称保留在 filename；显示名称和备注允许更新。
            for key in ("group_name", "stored", "filename", "created_at", "note"):
                source[key] = summary[key]
            return source

    def list_page(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        summaries = []
        with self._lock:
            for path in self.root.glob("upload_*/summary.json"):
                try:
                    summaries.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    logger.warning("无法读取上传结果集摘要：%s", path, exc_info=True)
        summaries.sort(key=lambda row: (row["created_at"], row["source_id"]), reverse=True)
        total = len(summaries)
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), page_count)
        start = (page - 1) * page_size
        return {
            "items": summaries[start:start + page_size], "total": total,
            "page": page, "page_size": page_size, "page_count": page_count,
        }

    def update(
        self, source_id: str, *, group_name: str | None = None, note: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            summary = self.summary(source_id)
            if group_name is not None:
                summary["group_name"] = group_name.strip() or summary["filename"]
            if note is not None:
                summary["note"] = note
            self._write_json(self._directory(source_id) / "summary.json", summary)
            return summary

    def delete(self, source_id: str) -> None:
        with self._lock:
            shutil.rmtree(self._directory(source_id))
