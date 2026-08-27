"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8503）
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..config import load_config
from ..media import probe_duration
from ..paths import RUNS_DIR
from .parse_input import Mode, parse_csv, parse_jsonl, parse_text
from .history import (
    build_xlsx,
    delete_snapshot,
    export_rows,
    list_snapshots,
    load_item_judge_calls,
    load_snapshot,
    result_export_row,
    rows_to_csv,
    save_task,
    snapshot_payload,
    task_to_snapshot,
    write_frames_zip,
)
from .video_prepare import (
    VIDEO_EXTENSIONS,
    resolve_operation_video_path,
)
from .runner import run_eval, run_update_batch, spawn_background
from .tasks import (
    TASKS,
    get_task,
    get_task_async,
    merge_items_by_id,
    new_task,
    peek_task,
    peek_task_async,
)

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
    csv: str | None = None


class EvalReq(BaseModel):
    mode: Mode
    items: list[dict]
    options: dict = {}
    dataset_name: str = ""


class EvalItemsReq(BaseModel):
    """向已有 task 批量更新/追加 items 的请求（task_id 等全部走 Body 参数）。"""

    task_id: str
    mode: Mode | None = None  # 更新已有任务可省略；新建任务必填；不一致 422
    items: list[dict]
    options: dict = {}
    dataset_name: str = ""  # 仅新建时生效，更新时忽略


class HistoryNoteReq(BaseModel):
    note: str = ""


_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS


def _resolve_operation_video_path(raw_path: str) -> Path:
    return resolve_operation_video_path(raw_path, base_dir=BASE_DIR)


def _validate_eval_request(req: EvalReq, app_cfg) -> None:
    """提交前校验：compare 模式每条必须有 video1 和 video2。"""
    selected = req.options.get("judges") or (
        [app_cfg.judges[0].name] if app_cfg.judges else []
    )
    selected_judges = [judge for judge in app_cfg.judges if judge.name in selected]
    if not selected_judges:
        selected_judges = app_cfg.judges[:1]
    if req.mode != "compare" or not selected_judges:
        return
    missing = [
        index + 1
        for index, item in enumerate(req.items)
        if not str(item.get("video1") or "").strip()
        or not str(item.get("video2") or "").strip()
    ]
    if missing:
        preview = "、".join(map(str, missing[:8]))
        suffix = "…" if len(missing) > 8 else ""
        raise HTTPException(
            422,
            f"垂域视觉对比每道题需要 video1 和 video2 视频路径，"
            f"第 {preview}{suffix} 条缺少视频路径。",
        )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# task_id / item id 允许所有字符（含中文/符号），仅限长度防文件名超长。
_MAX_ID_LENGTH = 128


def _validate_param_id(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(422, f"{label}不能为空")
    if len(value) > _MAX_ID_LENGTH:
        raise HTTPException(422, f"{label}不能超过 {_MAX_ID_LENGTH} 个字符")
    return value


def _validate_batch_item_ids(items: list[dict]) -> None:
    """更新批：所有条目必须携带非空字符串 id，且批内唯一。"""
    seen: set[str] = set()
    for pos, item in enumerate(items, 1):
        iid = item.get("id")
        if not isinstance(iid, str) or not iid.strip():
            raise HTTPException(422, f"第 {pos} 条缺少非空字符串 id")
        if len(iid) > _MAX_ID_LENGTH:
            raise HTTPException(
                422, f"第 {pos} 条 id 超过 {_MAX_ID_LENGTH} 个字符"
            )
        if iid in seen:
            raise HTTPException(422, f"id 重复：{iid}")
        seen.add(iid)


@app.get("/api/config")
def api_config():
    c = cfg()
    return {
        "judges": [
            {"name": j.name, "display": j.display or j.name}
            for j in c.judges
        ],
    }


@app.post("/api/parse")
def api_parse(req: ParseReq):
    if req.csv:
        items, errs = parse_csv(req.csv, req.mode)
    elif req.jsonl:
        items, errs = parse_jsonl(req.jsonl, req.mode)
    elif req.text is not None:
        items, errs = parse_text(req.text, req.mode)
    else:
        raise HTTPException(400, "需提供 text、jsonl 或 csv")
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
    task.active_runs += 1  # R1：提交时同步 pin（spawn 之前，无 await 间隙），消除启动延迟窗口内被 DELETE/LRU 淘汰的竞态

    async def _start_later():
        # 先把 task_id 响应给前端，再启动可能较重的评估任务；
        # 避免后台裁判/工具调用抢占事件循环，导致 /api/eval 本身迟迟不返回。
        await asyncio.sleep(0.05)
        await run_eval(task, app_cfg)

    spawn_background(_start_later())
    return {"task_id": task.id}


@app.post("/api/eval/items")
async def api_eval_items(req: EvalItemsReq):
    """向 task 批量更新/追加 items：按 id 匹配，命中原位替换、未命中追加末尾。

    task_id 不存在则用传入 id 新建任务（upsert）。本次全部 items 作为一个
    串行会话在后台评测（批次间并行、后完成者按 index 覆盖）；接口不流式，
    立即返回合并摘要，结果经 GET /api/eval/item/result 轮询获取。
    """
    task_id = _validate_param_id(req.task_id, "task_id")
    if not req.items:
        raise HTTPException(400, "items 为空")
    _validate_batch_item_ids(req.items)
    app_cfg = cfg()
    task = get_task(task_id)
    created = task is None
    mode = req.mode if created else task.mode
    if created:
        if req.mode is None:
            raise HTTPException(422, "新建任务必须提供 mode")
    elif req.mode is not None and req.mode != task.mode:
        raise HTTPException(
            422, f"mode 与任务不一致：任务为 {task.mode}，请求为 {req.mode}"
        )
    # compare 模式校验只针对本次批次 items（命中替换的条目也全部重评），
    # 放在 new_task 之前，避免校验失败留下空任务。
    _validate_eval_request(
        EvalReq(mode=mode, items=req.items, options=req.options), app_cfg
    )
    if created:
        task = new_task(
            mode,
            [],
            dict(req.options),
            dataset_name=req.dataset_name.strip(),
            task_id=task_id,
        )
    batch, replaced_ids, added_ids = merge_items_by_id(task, req.items)
    save_task(task)  # items 定义立即落快照；结果仍在各题完成后才覆盖合并
    effective_options = {**task.options, **req.options}
    task.active_runs += 1  # R1：提交时同步 pin（同 api_eval；run_update_batch 的 finally 负责解除）

    async def _start_later():
        # 先把合并结果响应出去，再启动可能较重的评测（与 /api/eval 同模式）。
        await asyncio.sleep(0.05)
        await run_update_batch(
            task,
            app_cfg,
            batch,
            options=effective_options,
            manage_status=created,
        )

    spawn_background(_start_later())
    return {
        "task_id": task.id,
        "created": created,
        "replaced_ids": replaced_ids,
        "added_ids": added_ids,
        "total_items": len(task.items),
    }


@app.get("/api/eval/item/result")
async def api_item_result(task_id: str, item_id: str):
    """查询单条 item 的当前结果：重评中返回既有旧结果并带 evaluating 标志。

    result 与 xlsx/CSV「逐题结果」同名列、同转换；多余字段不返回，
    失败结果额外附 error。
    """
    # R6：原同步 def 在线程池执行，get_task 的注册/容量迭代与事件循环线程
    # 并发变异共享 OrderedDict；改异步后 TASKS 变异统一在循环线程。
    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    idx = next(
        (i for i, it in enumerate(task.items) if it.get("id") == item_id), None
    )
    if idx is None:
        raise HTTPException(404, "item not found")
    evaluating = idx in task.in_flight_indexes
    # 从后往前取该 index 最新一条（历史可能存在同 index 重复条目）
    raw = next(
        (r for r in reversed(task.results) if r.get("index") == idx), None
    )
    if evaluating:
        status = "evaluating"
    elif raw is None:
        status = "pending"
    elif raw.get("error"):
        status = "failed"
    else:
        status = "done"
    return {
        "task_id": task.id,
        "item_id": item_id,
        "index": idx,
        "item": task.items[idx],
        "result": (
            result_export_row(task.mode, raw, idx, task.items)
            if raw is not None
            else None
        ),
        "evaluating": evaluating,
        "status": status,
    }


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 模块常量便于测试 monkeypatch
_UPLOAD_CHUNK = 1024 * 1024


@app.post("/api/upload/video")
async def api_upload_video(file: UploadFile = File(...)):
    """上传视觉评估录屏；延迟到开始评估时使用 rich_content 专用参数抽帧。

    分块流式落盘并边写边计数，超限即刻中断——避免整个文件先读入内存
    （旧实现在 read() 之后才检查大小，20MB 限制对大文件形同虚设）。
    """
    video_dir = RUNS_DIR / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "v.mp4").suffix.lower() or ".mp4"
    video_path = video_dir / f"{video_id}{suffix}"
    written = 0
    try:
        with video_path.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "视频过大，限制 ≤20MB")
                out.write(chunk)
    except Exception:
        video_path.unlink(missing_ok=True)  # 清理半截文件（413 与 IO 异常 alike）
        raise
    finally:
        await file.close()
    duration = probe_duration(video_path)
    return {
        "video_id": video_id,
        "video_path": str(video_path),
        "frames": [],
        "frame_count": 0,
        "duration": round(duration, 2),
    }


@app.get("/api/eval/{task_id}/stream")
async def api_stream(task_id: str):
    # 只读视图：终态任务回放完即返回不驻留；运行中任务 peek 命中活对象，订阅正常
    task = peek_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    async def event_gen():
        # 先订阅再回放：回放期间新产生的事件进入本连接队列，断线重连/中途
        # 打开都不丢增量（可能与回放重复推送一次，前端按 index 覆盖渲染，
        # 幂等）。旧实现的共享单队列还有多连接互相瓜分事件的正确性问题。
        q = task.subscribe()
        try:
            # 回放有界事件历史，供 Web 展示与文件日志同源的逐行调用记录。
            # 内层列表同样快照（R8）：yield 挂起期间 _record_progress 会并发
            # append/del 同一列表，遍历活列表会跳帧/重帧。
            for item_events in list(task.progress_events.values()):
                for progress_event in list(item_events):
                    yield _sse("progress_event", progress_event)
            # 回放每题最新进度，断线重连后能立即恢复当前阶段。
            for progress_item in list(task.item_progress.values()):
                yield _sse("item_progress", progress_item)
            # 先回放已有结果（断线重连不丢已完成的）
            for r in list(task.results):
                yield _sse("result", {"progress": task.done_total, "total": len(task.items), "result": r})
            # 终态判定叠加 active_runs（R4）：更新批 manage_status=False 全程
            # status=done，仅看 status 会在批运行中立即下发伪 done；批结束时
            # 由 run_update_batch 补发终态事件驱动下方实时循环退出。
            if task.status == "done" and task.active_runs <= 0:
                yield _sse("done", {"summary": task.summary, "total": len(task.items)})
                return
            if task.status == "error" and task.active_runs <= 0:
                yield _sse("error", {"message": task.error})
                return
            # 实时跟进
            while True:
                msg = await q.get()
                yield _sse(msg["event"], msg["data"])
                if msg["event"] in ("done", "error"):
                    break
        finally:
            task.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/history")
def api_history(limit: int = 50):
    return {"items": list_snapshots(limit=limit)}


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: str):
    # peek 不注册：浏览历史不再把整份快照读进内存永久驻留（旧的 get_task
    # miss 路径会注册驻留，翻 N 个历史内存就涨 N 份快照）。
    # touch=False（R6）：同步 def 在线程池执行，跳过 move_to_end 以保证
    # 完全不变异 TASKS（纯 dict 读在 GIL 下安全）。
    task = peek_task(task_id, touch=False)
    if not task:
        raise HTTPException(404, "task not found")
    return snapshot_payload(task_to_snapshot(task))


@app.delete("/api/history/{task_id}")
async def api_history_delete(task_id: str):
    # R6：TASKS 增删必须发生在事件循环线程（原同步 def 在线程池执行，与
    # move_to_end/_enforce_capacity 迭代竞争触发 RuntimeError）。全程无
    # await：检查-删盘-清内存相对提交端点的同步 pin（同样无 await 前置）
    # 是原子的，堵住删除与运行竞态；两个 unlink 的阻塞可忽略。
    task = TASKS.get(task_id)
    if task and (task.active_runs > 0 or task.status in {"pending", "running"}):
        raise HTTPException(409, "任务运行中，请等待完成后再删除")
    if not delete_snapshot(task_id):
        raise HTTPException(404, "task not found")
    if task is not None:
        TASKS.pop(task_id, None)  # 删除历史同步清内存副本（旧行为只删盘不删内存）
    return {"ok": True}


@app.patch("/api/history/{task_id}/note")
async def api_history_note(task_id: str, req: HistoryNoteReq):
    # peek 先查注册表：运行中返回活对象，改 note 落盘不丢并发结果；
    # 终态任务用临时对象改写，不驻留。R6：异步视图 + 落盘放 to_thread，
    # TASKS 触碰留在循环线程。
    task = await peek_task_async(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    note = req.note.strip()
    if len(note) > 1000:
        raise HTTPException(422, "备注不能超过 1000 个字符")
    task.note = note
    if not await asyncio.to_thread(save_task, task):
        raise HTTPException(500, "备注保存失败")
    return {"ok": True, "task_id": task.id, "note": task.note}


@app.get("/api/eval/{task_id}/export")
def api_export(task_id: str, format: str = "json"):
    task = peek_task(task_id, touch=False)  # 导出只读视图不驻留内存；touch=False：线程池端点不变异 TASKS
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")

    if format == "json":
        return JSONResponse(snapshot_payload(data))

    if format == "xlsx":
        content = build_xlsx(data)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.xlsx"},
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

    sheets = export_rows(data)
    csv_text = rows_to_csv(sheets.get("逐题结果") or [])
    return StreamingResponse(
        iter([csv_text.encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.csv"},
    )


def _download_stem(value: str, fallback: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_. " else "_"
        for char in value
    ).strip(" ._")
    return safe[:120] or fallback


@app.get("/api/eval/{task_id}/items/{item_index}/export")
def api_export_item(task_id: str, item_index: int, format: str):
    """导出单条结果关联的原视频、关键帧或裁判调用 JSON。"""
    task = peek_task(task_id, touch=False)  # 导出只读视图不驻留内存；touch=False：线程池端点不变异 TASKS
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

    uvicorn.run(app, host="0.0.0.0", port=8503)
