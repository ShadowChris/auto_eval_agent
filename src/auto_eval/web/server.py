"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8501）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_config
from .parse_input import Mode, parse_jsonl, parse_text
from .history import build_xlsx, delete_snapshot, export_rows, list_snapshots, load_snapshot, rows_to_csv, snapshot_payload, task_to_snapshot
from .runner import run_eval
from .tasks import get_task, new_task

# auto_eval_agent/ 目录（src/auto_eval/web/server.py 往上 4 层）
BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv(BASE_DIR / ".env")  # 注入 .env 的 key（KIMI_API_KEY/TAVILY_API_KEY 等）到环境变量

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
    task = new_task(req.mode, req.items, req.options)
    import asyncio

    asyncio.create_task(run_eval(task, cfg()))
    return {"task_id": task.id}


@app.get("/api/eval/{task_id}/stream")
async def api_stream(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    async def event_gen():
        # 先回放已有结果（断线重连不丢已完成的）
        for r in task.results:
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
        content = build_xlsx(data)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.xlsx"},
        )

    sheets = export_rows(data)
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

    uvicorn.run(app, host="0.0.0.0", port=8501)
