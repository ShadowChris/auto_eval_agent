"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8501）
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_config
from .parse_input import Mode, parse_jsonl, parse_text
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


@app.get("/api/eval/{task_id}/export")
def api_export(task_id: str, format: str = "json"):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if format == "json":
        return JSONResponse(
            {"task_id": task.id, "mode": task.mode, "results": task.results, "summary": task.summary}
        )
    out = io.StringIO()
    keys = sorted({k for r in task.results for k in r.keys()})
    writer = csv.DictWriter(out, fieldnames=keys)
    writer.writeheader()
    for r in task.results:
        row = {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in r.items()
        }
        writer.writerow(row)
    return StreamingResponse(
        iter([out.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=eval_{task.id}.csv"},
    )


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8501)
