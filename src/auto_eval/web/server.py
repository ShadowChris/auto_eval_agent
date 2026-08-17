"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8501）
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware

from ..config import ExpertKnowledgeBase, load_config
from ..expert_knowledge import ExpertKnowledgeStore, render_expert_knowledge
from ..media import extract_scene_keyframes, probe_duration
from ..paths import RUNS_DIR
from .parse_input import Mode, parse_jsonl, parse_text
from .history import (
    build_xlsx,
    delete_snapshot,
    export_rows,
    jsonl_export_rows,
    list_snapshots,
    list_snapshots_page,
    load_item_judge_calls,
    load_snapshot,
    rows_to_csv,
    rows_to_jsonl,
    save_task,
    snapshot_payload,
    task_to_snapshot,
    write_frames_zip,
)
from .operation_media import (
    VIDEO_EXTENSIONS,
    operation_video_roots,
    prepare_cached_operation_item,
    resolve_operation_video_path,
)
from .runner import run_eval
from .tasks import get_live_task, get_task, new_task

# auto_eval_agent/ 目录（src/auto_eval/web/server.py 往上 4 层）
BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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
    options: dict = {}
    dataset_name: str = ""


class OperationPrepareReq(BaseModel):
    items: list[dict]
    concurrency: int = 2


class HistoryNoteReq(BaseModel):
    note: str = ""


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


def _sse(event: str, data, *, event_id: int | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


@app.post("/api/eval")
async def api_eval(req: EvalReq):
    if not req.items:
        raise HTTPException(400, "items 为空")
    app_cfg = cfg()
    _validate_eval_request(req, app_cfg)
    task = new_task(
        req.mode,
        req.items,
        req.options,
        dataset_name=req.dataset_name.strip(),
    )
    async def _start_later():
        # 先把 task_id 响应给前端，再启动可能较重的评估任务；
        # 避免后台裁判/工具调用抢占事件循环，导致 /api/eval 本身迟迟不返回。
        await asyncio.sleep(0.05)
        await run_eval(task, app_cfg)

    execution = asyncio.create_task(_start_later())
    task.execution = execution

    def clear_execution(finished: asyncio.Task) -> None:
        if task.execution is finished:
            task.execution = None

    execution.add_done_callback(clear_execution)
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
                    "progress": max(task.done_total, len(task.results)),
                    "total": len(task.items),
                    "started_at": task.started_at,
                    "finished_at": task.finished_at,
                    "duration_s": task.elapsed_s(),
                    "error": task.error,
                },
            )
            delivered_cursor = max(0, replay_after or 0)
            terminal_replayed = False
            if replay_after is None:
                # 兼容旧客户端：没有事件游标时仍回放完整页面状态。
                for item_events in list(task.progress_events.values()):
                    for progress_event in item_events:
                        yield _sse("progress_event", progress_event)
                for progress_item in list(task.item_progress.values()):
                    yield _sse("item_progress", progress_item)
                for result in list(task.results):
                    yield _sse(
                        "result",
                        {
                            "progress": max(task.done_total, len(task.results)),
                            "total": len(task.items),
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
                        "done", "error", "cancelled",
                    }

            if task.status in {"done", "error", "cancelled"}:
                if not terminal_replayed:
                    if task.status == "done":
                        yield _sse(
                            "done",
                            {
                                "summary": task.summary,
                                "total": len(task.items),
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
                if msg["event"] in ("done", "error", "cancelled"):
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
            "total": len(live.items),
            "done": max(live.done_total, len(live.results)),
            "started_at": live.started_at,
            "finished_at": live.finished_at,
            "duration_s": live.elapsed_s(),
            "error": live.error,
        })
    return {
        "items": rows,
        "total": history_total,
        "page": max(1, int(page or 1)),
        "page_size": response_page_size,
    }


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: str, compact: bool = False):
    task = get_task(task_id)
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


@app.delete("/api/history/{task_id}")
def api_history_delete(task_id: str):
    live = get_live_task(task_id)
    if live is not None and live.status in {"pending", "running"}:
        raise HTTPException(409, "运行中的任务请先中断，再删除历史记录")
    if not delete_snapshot(task_id):
        raise HTTPException(404, "task not found")
    return {"ok": True}


@app.post("/api/eval/{task_id}/cancel")
async def api_eval_cancel(task_id: str):
    task = get_live_task(task_id)
    if task is None:
        raise HTTPException(404, "当前服务中没有这个运行任务")
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
    await task.publish(
        "cancelled",
        {"message": reason, "duration_s": task.duration_s},
    )
    if not save_task(task):
        raise HTTPException(500, "任务已中断，但历史状态保存失败")
    return {"ok": True, "task_id": task.id, "status": task.status}


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


@app.get("/api/eval/{task_id}/export")
def api_export(task_id: str, format: str = "json"):
    task = get_task(task_id)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")

    if format == "json":
        return JSONResponse(snapshot_payload(data))

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
def api_export_item(task_id: str, item_index: int, format: str):
    """导出单条结果关联的原视频、关键帧或裁判调用 JSON。"""
    task = get_task(task_id)
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

    raise HTTPException(400, "format 必须是 video、frames_zip 或 judge_calls")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8502)
