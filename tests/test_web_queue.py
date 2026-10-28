"""任务级单消费者 FIFO 队列：全量评测/重跑一次跑一张表，多张排队可取消。"""
import asyncio
from pathlib import Path

from auto_eval.config import load_config
from auto_eval.web import runner, server
from auto_eval.web.scheduler import EVAL_SCHEDULER
from auto_eval.web.tasks import TASKS, Task

_CFG = load_config(Path("config"))


async def _drain(timeout: float = 5.0) -> None:
    """等待单消费者 FIFO 队列处理完当前所有 job。"""
    async def _idle() -> None:
        while True:
            snap = EVAL_SCHEDULER.snapshot()
            if not snap["running"] and not snap["queued"]:
                return
            await asyncio.sleep(0.005)
    await asyncio.wait_for(_idle(), timeout)


def _task(task_id: str) -> Task:
    return Task(id=task_id, mode="rich_content", items=[{"id": "x", "query": "q"}], options={})


async def test_fifo_runs_one_task_at_a_time():
    """连续入队两张表：位次 1/2；第一张严格跑完才弹第二张；都收敛 done。"""
    order: list[str] = []
    ran: set[str] = set()

    async def runner(task, cfg):
        order.append(f"start:{task.id}")
        ran.add(task.id)
        await asyncio.sleep(0.02)
        order.append(f"finish:{task.id}")
        # 模拟 run_eval / run_update_batch 的 finally：解除 enqueue 的 pin
        task.active_runs = max(0, task.active_runs - 1)
        task.status = "done"

    t1, t2 = _task("qA"), _task("qB")
    try:
        p1 = EVAL_SCHEDULER.enqueue(t1, object(), runner)
        p2 = EVAL_SCHEDULER.enqueue(t2, object(), runner)
        assert (p1, p2) == (1, 2)
        assert t1.status == "queued" and t2.status == "queued"
        assert [q["queue_position"] for q in EVAL_SCHEDULER.snapshot()["queued"]] == [1, 2]
        await _drain()
        assert order == ["start:qA", "finish:qA", "start:qB", "finish:qB"]  # 严格 FIFO
        assert ran == {"qA", "qB"}
        assert t1.status == "done" and t2.status == "done"
    finally:
        await EVAL_SCHEDULER.stop()
        for t in (t1, t2):
            TASKS.pop(t.id, None)


async def test_cancel_queued_removes_without_running():
    """排队中的任务可取消；跑完/运行中的不可按排队任务取消。"""
    started = asyncio.Event()

    async def runner(task, cfg):
        started.set()
        await asyncio.sleep(0.01)
        task.active_runs = max(0, task.active_runs - 1)
        task.status = "done"

    t1, t2 = _task("cA"), _task("cB")
    try:
        EVAL_SCHEDULER.enqueue(t1, object(), runner)
        EVAL_SCHEDULER.enqueue(t2, object(), runner)
        # t2 还在排队：取消应移除并停止占用 pin
        cancelled = EVAL_SCHEDULER.cancel(t2.id)
        assert cancelled is t2
        assert t2.status == "cancelled"
        assert t2.active_runs == 0
        # 只 t1 实际开跑
        await asyncio.wait_for(started.wait(), 2)
        snap = EVAL_SCHEDULER.snapshot()
        assert snap["running"] and snap["running"]["task_id"] == "cA"
        assert [q["task_id"] for q in snap["queued"]] == []
        await _drain()
        assert t1.status == "done"
        # 已跑完/不在排队：不可按排队任务取消
        assert EVAL_SCHEDULER.cancel(t1.id) is None
    finally:
        await EVAL_SCHEDULER.stop()
        for t in (t1, t2):
            TASKS.pop(t.id, None)

async def test_api_eval_submits_enqueue_and_runs_to_done(monkeypatch):
    """端到端：POST /api/eval 走单消费者队列，两张表排队、依次跑完均 done。

    避免命中真实网络/视频：patch 裁判客户端与 _eval_one。
    """
    class Client:
        def __init__(self, cfg):
            self.config = cfg

        async def aclose(self):
            pass

    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        return {"index": idx, "item_id": item.get("id"), "query": item.get("query")}

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *a: None)
    monkeypatch.setattr(runner, "_eval_one", fake_eval_one)
    # 不 patch _persist_task：任务跑完 retire 后从快照回载，需真实落盘才能看到 done。
    old_cfg = server._state.get("cfg")
    server._state["cfg"] = _CFG
    try:
        item = {"id": "a", "query": "q", "frames": ["/tmp/x/kf.jpg"]}
        r1 = await server.api_eval(server.EvalReq(mode="rich_content", items=[item], options={}))
        r2 = await server.api_eval(server.EvalReq(mode="rich_content", items=[item], options={}))
        assert r1["status"] == "queued" and r1["queue_position"] == 1
        assert r2["status"] == "queued" and r2["queue_position"] == 2
        snap = EVAL_SCHEDULER.snapshot()
        assert [q["queue_position"] for q in snap["queued"]] == [1, 2]
        # 等两个 job 都跑完
        await _drain(5)
        for tid in (r1["task_id"], r2["task_id"]):
            t = TASKS.get(tid) or server.peek_task(tid)
            assert t is not None and t.status == "done"
        assert len(calls) == 2  # 两张表各一路 item 都评了
    finally:
        await EVAL_SCHEDULER.stop()
        server._state["cfg"] = old_cfg
        for tid in (r1["task_id"], r2["task_id"]):
            TASKS.pop(tid, None)
