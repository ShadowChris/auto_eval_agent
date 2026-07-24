"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8501）
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_config
from ..media import extract_scene_keyframes, probe_duration
from ..paths import RUNS_DIR
from .parse_input import Mode, parse_jsonl, parse_text
from .history import build_xlsx, delete_snapshot, export_rows, list_snapshots, load_snapshot, rows_to_csv, snapshot_payload, task_to_snapshot
from .operation_media import (
    VIDEO_EXTENSIONS,
    operation_video_roots,
    prepare_cached_operation_item,
    resolve_operation_video_path,
)
from .runner import run_eval
from .tasks import get_task, new_task

# auto_eval_agent/ 目录（src/auto_eval/web/server.py 往上 4 层）
BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv(BASE_DIR / ".env", override=True)  # 注入 .env 的 key；以 .env 为准覆盖旧 shell 环境变量

app = FastAPI(title="auto_eval 评估台")
_state: dict = {}


@app.on_event("startup")
def _load():
    _state["cfg"] = load_config(CONFIG_DIR)


def cfg():
    return _state["cfg"]


class ParseReq(BaseModel):
    mode: Mode
    text: str | None = None
    jsonl: str | None = None


class EvalReq(BaseModel):
    mode: Mode
    items: list[dict]
    options: dict = {}


class OperationPrepareReq(BaseModel):
    items: list[dict]
    concurrency: int = 2


_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS


def _operation_video_roots() -> list[Path]:
    return operation_video_roots(BASE_DIR)


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


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/config")
def api_config():
    c = cfg()
    return {
        "judges": [
            {"name": j.name, "display": j.display or j.name, "persona": j.persona, "enable_web_search": j.enable_web_search}
            for j in c.judges
        ],
        "models": [m.name for m in c.models],
        "rubrics": [d.name for d in c.rubrics],
        "scale": c.rubrics[0].scale if c.rubrics else 5,
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


@app.post("/api/eval")
async def api_eval(req: EvalReq):
    if not req.items:
        raise HTTPException(400, "items 为空")
    app_cfg = cfg()
    _validate_eval_request(req, app_cfg)
    task = new_task(req.mode, req.items, req.options)
    async def _start_later():
        # 先把 task_id 响应给前端，再启动可能较重的评估任务；
        # 避免后台裁判/工具调用抢占事件循环，导致 /api/eval 本身迟迟不返回。
        await asyncio.sleep(0.05)
        await run_eval(task, app_cfg)

    asyncio.create_task(_start_later())
    return {"task_id": task.id}


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


@app.get("/api/eval/{task_id}/stream")
async def api_stream(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    async def event_gen():
        # 清掉连接建立前已进入队列的快照类事件；下面统一回放最新状态，
        # 避免先回放 60% 后又消费旧队列事件退回到 10%。
        while True:
            try:
                task.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # 回放有界事件历史，供 Web 展示与文件日志同源的逐行调用记录。
        for item_events in list(task.progress_events.values()):
            for progress_event in item_events:
                yield _sse("progress_event", progress_event)
        # 回放每题最新进度，断线重连后能立即恢复当前阶段。
        for progress_item in list(task.item_progress.values()):
            yield _sse("item_progress", progress_item)
        # 先回放已有结果（断线重连不丢已完成的）
        for r in list(task.results):
            yield _sse("result", {"progress": task.done_total, "total": len(task.items), "result": r})
        if task.status == "done":
            yield _sse("done", {"summary": task.summary, "total": len(task.items)})
            return
        if task.status == "error":
            yield _sse("error", {"message": task.error})
            return
        # 实时跟进
        while True:
            msg = await task.queue.get()
            yield _sse(msg["event"], msg["data"])
            if msg["event"] in ("done", "error"):
                break

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/history")
def api_history(limit: int = 50):
    return {"items": list_snapshots(limit=limit)}


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: str):
    task = get_task(task_id)
    if task:
        return snapshot_payload(task_to_snapshot(task))
    data = load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    return snapshot_payload(data)


@app.delete("/api/history/{task_id}")
def api_history_delete(task_id: str):
    if not delete_snapshot(task_id):
        raise HTTPException(404, "task not found")
    return {"ok": True}


@app.get("/api/eval/{task_id}/export")
def api_export(task_id: str, format: str = "json"):
    task = get_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")

    if format == "json":
        return JSONResponse(snapshot_payload(data))

    if format == "xlsx":
        content = build_xlsx(data, cfg())
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.xlsx"},
        )

    sheets = export_rows(data, cfg())
    csv_text = rows_to_csv(sheets.get("逐题结果") or [])
    return StreamingResponse(
        iter([csv_text.encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.csv"},
    )

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8503)
