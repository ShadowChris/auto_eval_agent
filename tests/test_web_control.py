"""运行控制：暂停 / 继续 / 停止。门控语义、批/全量评测的终止、API 校验、前端接线。"""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_eval.config import load_config
from auto_eval.web import runner, server
from auto_eval.web.scheduler import ResizableLimiter
from auto_eval.web.tasks import EvalStopped, TASKS, Task

from test_web_scheduler import _patch_runner, _reset_scheduler_globals

_CFG = load_config(Path("config"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "src/auto_eval/web/static/app.js"
INDEX_HTML = PROJECT_ROOT / "src/auto_eval/web/static/index.html"


def _task(n: int) -> Task:
    return Task(
        id="ctl-test",
        mode="rich_content",
        items=[
            {"id": f"s{i}", "query": f"题{i}", "frames": ["/tmp/f_kf.jpg"]}
            for i in range(n)
        ],
        options={"judges": [_CFG.judges[0].name]},
    )


def _free_task(n: int = 3) -> Task:
    """控制用任务：无帧（走预处理），fake prep 无需真实视频。"""
    return Task(
        id=f"ctl-{n}",
        mode="rich_content",
        items=[{"id": f"s{i}", "query": f"题{i}"} for i in range(n)],
        options={"judges": [_CFG.judges[0].name]},
    )


def _drain_events(q: asyncio.Queue) -> list[dict]:
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


def _patch_video_prep(monkeypatch):
    def fake_prep(item, **kw):
        return {**item, "frames": ["/tmp/fake/kf_001.jpg"], "frame_count": 1}

    monkeypatch.setattr(runner, "prepare_session_rich_content_item", fake_prep)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", fake_prep)


# ---------- Task 门控原语 ----------

async def test_wait_runnable_direct_pass_when_not_paused():
    task = Task(id="t1", mode="rich_content", items=[], options={})
    await task.wait_runnable()  # 未暂停/未停止：立即返回


async def test_pause_blocks_wait_runnable_until_resume():
    task = Task(id="t2", mode="rich_content", items=[], options={})
    task.pause()
    waiter = asyncio.create_task(task.wait_runnable())
    await asyncio.sleep(0.05)  # 暂停中阻塞（含挂起点，非 eager 退化）
    assert not waiter.done()
    task.resume()
    await asyncio.wait_for(waiter, 1)  # 恢复后放行
    assert not task.stopped


async def test_stop_raises_evalstopped_from_wait_runnable():
    task = Task(id="t3", mode="rich_content", items=[], options={})
    task.pause()  # 停在门内
    waiter = asyncio.create_task(task.wait_runnable())
    await asyncio.sleep(0.05)
    task.stop()  # 停止唤醒暂停等待者 → EvalStopped
    with pytest.raises(EvalStopped):
        await asyncio.wait_for(waiter, 1)


async def test_stop_when_not_paused_raises_immediately():
    task = Task(id="t4", mode="rich_content", items=[], options={})
    task.stop()
    with pytest.raises(EvalStopped):
        await task.wait_runnable()


# ---------- API 校验（404 / 409 / 状态迁移） ----------

def test_control_endpoints_validation(monkeypatch):
    task = _task(1)
    TASKS[task.id] = task
    try:
        with TestClient(server.app) as client:
            # 未在评测（active_runs=0）→ 409；不存在 → 404
            assert client.post(f"/api/eval/{task.id}/pause").status_code == 409
            assert client.post(f"/api/eval/{task.id}/stop").status_code == 409
            assert client.post("/api/eval/no-such/pause").status_code == 404

            task.active_runs = 1
            task.status = "running"
            assert client.post(f"/api/eval/{task.id}/pause").status_code == 200
            assert task.paused
            assert client.post(f"/api/eval/{task.id}/resume").status_code == 200
            assert not task.paused
            assert client.post(f"/api/eval/{task.id}/pause").status_code == 200
            # 暂停中停止：唤醒 + 终态
            assert client.post(f"/api/eval/{task.id}/stop").status_code == 200
            assert task.stopped and task.paused
            # 停止后不可继续
            assert client.post(f"/api/eval/{task.id}/resume").status_code == 409
    finally:
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


def test_control_endpoint_publishes_control_event(monkeypatch):
    task = _task(1)
    task.active_runs = 1
    task.status = "running"
    TASKS[task.id] = task
    q = task.subscribe()
    try:
        with TestClient(server.app) as client:
            client.post(f"/api/eval/{task.id}/stop")
        events = _drain_events(q)
        assert events[-1]["event"] == "control"
        assert events[-1]["data"]["stopped"] is True
    finally:
        task.unsubscribe(q)
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


def test_history_detail_exposes_control_state(monkeypatch):
    task = _task(1)
    task.active_runs = 1
    task.status = "running"
    task.paused = True
    TASKS[task.id] = task
    try:
        with TestClient(server.app) as client:
            payload = client.get(f"/api/history/{task.id}").json()
        assert payload["paused"] is True
        assert payload["stopped"] is False
    finally:
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


# ---------- 暂停：批口径精确门控 ----------

async def test_update_batch_pause_blocks_next_item_then_resume(monkeypatch):
    """暂停停在批内下一题边界，恢复后继续；已开始的题正常完成。"""
    task = _free_task(3)
    calls: list[int] = []
    blocked = asyncio.Event()

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        if idx == 0:
            await blocked.wait()  # 第一题占住（模拟真实模型调用）
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    _patch_video_prep(monkeypatch)
    batch = [(i, dict(task.items[i])) for i in range(3)]
    run = asyncio.create_task(
        runner._run_update_batch_body(task, _CFG, batch, options=task.options)
    )
    await asyncio.sleep(0.1)
    assert calls == [0]  # 第一题在跑
    task.pause()
    blocked.set()  # 第一题完成 → 批循环到第二题，应被暂停门挡住
    await asyncio.sleep(0.1)
    assert calls == [0]  # 暂停中第二题未启动
    task.resume()
    await asyncio.wait_for(run, 5)
    assert calls == [0, 1, 2]
    assert task.stopped is False


# ---------- 停止：全量评测终止 + 余项标停 + error 终态 ----------

async def test_run_eval_stop_marks_remaining_and_error_terminal(monkeypatch):
    """串行模型槽下 stop：在途题完成，排队/未启动的题落「评测已停止」，终态 error。"""
    task = _task(4)
    task.active_runs = 1
    task.status = "pending"
    calls: list[int] = []
    blocked = asyncio.Event()

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        if idx == 0:
            await blocked.wait()
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)  # 串行：仅 1 题在台面
    q = task.subscribe()
    run = asyncio.create_task(runner.run_eval(task, _CFG))
    try:
        await asyncio.sleep(0.1)
        assert calls == [0]  # 只有第 1 题在评测
        task.stop()
        blocked.set()  # 在途第 1 题完成；其余题拿到模型槽发现已停止 → 不再调模型
        await asyncio.wait_for(run, 5)

        events = _drain_events(q)
        assert events[0]["event"] == "start"
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["stopped"] is True
        assert "用户停止" in task.error
        assert task.status == "error"
        assert calls == [0]  # stop 后未再发起任何新模型调用
        # 每道题都有确定结果：在途的成功，其余「评测已停止」
        assert [r["index"] for r in task.results] == [0, 1, 2, 3]
        assert "error" not in task.results[0]
        for i in (1, 2, 3):
            assert task.results[i]["error"] == "评测已停止"
        # done_total 与结果行一致（含停止项，供汇总口径统计为 failed）
        assert task.done_total == 4
    finally:
        task.unsubscribe(q)
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


async def test_update_batch_stop_marks_remaining_and_error_terminal(monkeypatch):
    """批口径 stop：串行批当前轮在途完成，剩余条目标停，manage_status=True 出 error 终态。"""
    task = _free_task(3)
    calls: list[int] = []
    blocked = asyncio.Event()

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        if idx == 0:
            await blocked.wait()
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    _patch_video_prep(monkeypatch)
    task.active_runs = 1
    task.status = "pending"
    q = task.subscribe()
    batch = [(i, dict(task.items[i])) for i in range(3)]
    run = asyncio.create_task(
        runner.run_update_batch(
            task, _CFG, batch, options=task.options, manage_status=True
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert calls == [0]
        task.stop()
        blocked.set()
        await asyncio.wait_for(run, 5)
        assert calls == [0]
        assert task.status == "error"
        assert "用户停止" in (task.error or "")
        assert [r["index"] for r in task.results] == [0, 1, 2]
        assert "error" not in task.results[0]
        assert task.results[1]["error"] == "评测已停止"
        assert task.results[2]["error"] == "评测已停止"
        events = _drain_events(q)
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["stopped"] is True
    finally:
        task.unsubscribe(q)
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


# ---------- 前端接线（静态断言） ----------

def test_frontend_control_static_asserts():
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    assert "taskPaused = ref(false)" in app_js
    assert "taskStopped = ref(false)" in app_js
    assert 'addEventListener("control"' in app_js
    assert "/pause" in app_js and "/resume" in app_js and "/stop" in app_js
    assert "暂停" in index_html and "停止" in index_html
    assert "taskPaused ? resumeTask() : pauseTask()" in index_html
    assert "@click=\"stopTask\"" in index_html or "@click='stopTask'" in index_html