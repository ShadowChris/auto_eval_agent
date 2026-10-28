"""按题读取已保存的关键帧；与视频获取、抽帧算法及 ZIP 导出互不依赖。"""
from __future__ import annotations

from io import BytesIO
import json
import math
import re
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ..paths import PROJECT_ROOT, RUNS_DIR


def _groups(item: dict) -> list[dict]:
    variants = item.get("group_variants") or []
    if variants:
        return [{
            "group_id": variant.get("group_id") or str(index),
            "group_name": variant.get("group_name") or variant.get("group_id") or f"数据组 {index + 1}",
            "frames": (variant.get("item") or {}).get("frames") or [],
        } for index, variant in enumerate(variants)]
    return [{"group_id": "single", "group_name": "录屏", "frames": item.get("frames") or []}]


def _frame_path(raw: str, runs_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_relative_to(runs_dir.resolve()) or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(404, "该帧路径无效")
    return path


def _metadata(path: Path) -> dict[int, dict]:
    metadata_path = path.parent / "keyframes.json"
    try:
        if metadata_path.is_symlink() or metadata_path.stat().st_size > 2 * 1024 * 1024:
            return {}
        records = json.loads(metadata_path.read_text(encoding="utf-8")).get("selected", [])
        return {int(row["index"]): row for row in records}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def create_frame_preview_router(
    load: Callable[[str], dict | None], *, runs_dir: Path = RUNS_DIR,
) -> APIRouter:
    router = APIRouter()

    def get_item(task_id: str, item_index: int) -> dict:
        snapshot = load(task_id)
        if snapshot is None:
            raise HTTPException(404, "历史任务不存在")
        items = snapshot.get("items") or []
        if item_index < 0 or item_index >= len(items):
            raise HTTPException(404, "题目不存在")
        return items[item_index]

    @router.get("/api/eval/{task_id}/items/{item_index}/frames")
    def frame_manifest(task_id: str, item_index: int) -> dict:
        item = get_item(task_id, item_index)
        result = []
        for group_index, group in enumerate(_groups(item)):
            records = []
            metadata_by_dir = {}
            for index, raw in enumerate(group["frames"]):
                try:
                    path = _frame_path(str(raw), runs_dir)
                except HTTPException:
                    records.append({"index": index, "filename": f"帧 {index + 1}", "available": False, "time": None})
                    continue
                if path.parent not in metadata_by_dir:
                    metadata_by_dir[path.parent] = _metadata(path)
                match = re.fullmatch(r"kf_(\d+)", path.stem)
                info = metadata_by_dir[path.parent].get(int(match[1]) if match else index + 1, {})
                timestamp = info.get("time")
                timestamp = timestamp if isinstance(timestamp, (int, float)) and math.isfinite(timestamp) else None
                try:
                    version = path.stat().st_mtime_ns if path.is_file() else 0
                except OSError:
                    version = 0
                records.append({
                    "index": index, "filename": path.name, "time": timestamp,
                    "available": path.is_file(),
                    "url": f"/api/eval/{task_id}/items/{item_index}/frames/{index}?group_index={group_index}&v={version}",
                })
            result.append({
                "group_id": group["group_id"], "group_name": group["group_name"],
                "frame_count": len(records), "frames": records,
            })
        return {
            "item_id": item.get("id") or str(item_index + 1), "query": item.get("query") or "",
            "total_count": sum(group["frame_count"] for group in result), "groups": result,
        }

    @router.get("/api/eval/{task_id}/items/{item_index}/frames/{frame_index}")
    def frame_image(
        task_id: str, item_index: int, frame_index: int,
        group_index: int = Query(0, ge=0), thumbnail: bool = False,
    ) -> Response:
        groups = _groups(get_item(task_id, item_index))
        if group_index >= len(groups):
            raise HTTPException(404, "数据组不存在")
        frames = groups[group_index]["frames"]
        if frame_index < 0 or frame_index >= len(frames):
            raise HTTPException(404, "帧不存在")
        path = _frame_path(str(frames[frame_index]), runs_dir)
        if not path.is_file():
            raise HTTPException(404, "该帧文件已丢失或被清理")
        headers = {"Cache-Control": "private, max-age=300"}
        if thumbnail:
            try:
                from PIL import Image

                with Image.open(path) as source:
                    image = source.convert("RGB")
                    image.thumbnail((144, 144))
                    content = BytesIO()
                    image.save(content, format="JPEG", quality=70)
                return Response(content.getvalue(), media_type="image/jpeg", headers=headers)
            except (OSError, ValueError) as exc:
                raise HTTPException(404, "该帧图片无法解析") from exc
        return FileResponse(path, headers=headers)

    return router
