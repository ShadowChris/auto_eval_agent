"""全局评测队列：两级可调限流器（模型调用级/流水线准入）、组优先续队、连坐失败、全局设置。"""
import asyncio
import contextlib
import time
from pathlib import Path

from auto_eval.config import load_config
from auto_eval.web import runner, scheduler
from auto_eval.web.scheduler import (
    DEFAULT_CONCURRENCY,
    DEFAULT_EVAL_TIMEOUT_S,
    DEFAULT_WAITING_CAPACITY,
    ResizableLimiter,
)
from auto_eval.web.tasks import Task


def _reset_scheduler_globals():
    scheduler.apply_settings(
        concurrency=DEFAULT_CONCURRENCY,
        waiting_capacity=DEFAULT_WAITING_CAPACITY,
        eval_timeout_s=DEFAULT_EVAL_TIMEOUT_S,
        judges=[],
    )


def _session_task(cfg, groups: dict[str, int], *, standalone: int = 0) -> Task:
    """构造 rich_content 任务：groups 为 {组名: 轮数}，各轮带 frames 跳过抽帧。"""
    items = []
    for grp, turns in groups.items():
        for turn in range(1, turns + 1):
            items.append({
                "id": f"{grp}_t{turn}",
                "query": f"{grp} 第{turn}轮",
                "frames": [f"/tmp/{grp}_{turn}/kf_001.jpg"],
                "session_group": grp,
                "turn_index": turn,
            })
    for i in range(standalone):
        items.append({
            "id": f"s{i}",
            "query": f"独立题{i}",
            "frames": [f"/tmp/standalone_{i}/kf_001.jpg"],
        })
    return Task(
        id="sched-test",
        mode="rich_content",
        items=items,
        options={"judges": [cfg.judges[0].name]},
    )


def _patch_runner(
    monkeypatch,
    model_limiter: ResizableLimiter,
    fake_eval_one,
    *,
    pipeline_limiter: ResizableLimiter | None = None,
):
    """patch 双限流器：首个参数是模型调用级限流器；流水线准入缺省
    = 模型上限 + DEFAULT_WAITING_CAPACITY（镜像生产默认，不干扰既有用例），
    需要考察等待容量时显式传 pipeline_limiter。"""
    monkeypatch.setattr(runner, "MODEL_LIMITER", model_limiter)
    monkeypatch.setattr(
        runner,
        "PIPELINE_LIMITER",
        pipeline_limiter or ResizableLimiter(
            model_limiter.limit + DEFAULT_WAITING_CAPACITY
        ),
    )
    monkeypatch.setattr(runner, "_eval_one", fake_eval_one)
    monkeypatch.setattr(runner, "_persist_task", lambda task, **kw: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **kw: None)


# ---------- ResizableLimiter ----------

async def test_limiter_fifo_order_and_admission_cap():
    lim = ResizableLimiter(2)
    order: list[int] = []
    active = 0
    peak = 0

    async def job(i: int):
        nonlocal active, peak
        async with lim:
            active += 1
            peak = max(peak, active)
            order.append(i)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(job(i) for i in range(5)))
    assert order == [0, 1, 2, 3, 4]
    assert peak == 2
    assert lim.stats() == {"limit": 2, "running": 0, "queued": 0}


async def test_limiter_resize_up_wakes_down_is_soft():
    lim = ResizableLimiter(1)
    await lim.acquire()
    queued = asyncio.create_task(lim.acquire())
    await asyncio.sleep(0)
    assert lim.stats()["queued"] == 1

    lim.set_limit(2)  # 调大：排队者立即放行
    await asyncio.sleep(0)
    assert queued.done() and not queued.cancelled()
    assert lim.stats()["running"] == 2

    lim.set_limit(1)  # 调小：软限制，不抢占运行中的两个持有者
    later = asyncio.create_task(lim.acquire())
    await asyncio.sleep(0.05)
    assert not later.done()
    lim.release()
    await asyncio.sleep(0.05)
    assert not later.done()  # 仍有一个持有者超发（running 1 == 新上限）
    lim.release()
    await asyncio.sleep(0.05)
    assert later.done()
    lim.release()  # later 归还
    assert lim.stats()["running"] == 0


async def test_limiter_cancelled_waiter_leaks_no_slot():
    lim = ResizableLimiter(1)
    await lim.acquire()
    queued = asyncio.create_task(lim.acquire())
    await asyncio.sleep(0)
    assert lim.stats()["queued"] == 1

    queued.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await queued
    assert lim.stats()["queued"] == 0

    lim.release()
    async with lim:  # 被取消的等待者不占用槽位，下一次立即可进
        assert lim.stats()["running"] == 1
    assert lim.stats()["running"] == 0


async def test_limiter_priority_acquire_inserts_at_head():
    """priority=True 插队头：后到的优先者先于先到的普通者被放行。"""
    lim = ResizableLimiter(1)
    await lim.acquire()
    order: list[str] = []

    async def waiter(tag: str, priority: bool):
        async with lim.slot(priority=priority):
            order.append(tag)

    normal = asyncio.create_task(waiter("normal", False))
    await asyncio.sleep(0)
    ahead = asyncio.create_task(waiter("priority", True))
    await asyncio.sleep(0)
    assert lim.stats()["queued"] == 2

    lim.release()  # 队头的优先者先拿到槽
    await asyncio.gather(normal, ahead)
    assert order == ["priority", "normal"]
    assert lim.stats() == {"limit": 1, "running": 0, "queued": 0}


async def test_limiter_priority_cancelled_waiter_leaks_no_slot():
    """priority 路径的取消清理与普通路径同等安全（镜像取消测试）。"""
    lim = ResizableLimiter(1)
    await lim.acquire()
    queued = asyncio.create_task(lim.acquire(priority=True))
    await asyncio.sleep(0)
    assert lim.stats()["queued"] == 1

    queued.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await queued
    assert lim.stats()["queued"] == 0

    lim.release()
    async with lim.slot(priority=True):
        assert lim.stats()["running"] == 1
    assert lim.stats()["running"] == 0


# ---------- 组优先续队 + 连坐失败（全量跑批） ----------

async def test_session_turns_priority_requeue_not_tail(monkeypatch):
    """模型槽按题获取 + 组第 2+ 轮插队头：组内轮次保持相对顺序、不排到
    新到达者之后；组间只让位于「释放时已在队的等待者」，交错一轮
    （limit=1 两组各 3 轮 → [0,3,1,4,2,5]，回归：第 2 轮排队尾）。"""
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {"g1": 3, "g2": 3})
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        # sleep(0) 模拟真实模型调用的挂起点：Python 3.14 eager task 下，
        # 不挂起的 fake 会让 wait_for 全程不 yield、任务完全串行
        await asyncio.sleep(0)
        calls.append(idx)
        return {
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"summary-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    await runner._run(task, cfg)

    assert calls == [0, 3, 1, 4, 2, 5]
    assert "【第1轮】summary-0" in task.items[1]["context"]
    assert "【第2轮】summary-1" in task.items[2]["context"]


async def test_group_second_turn_beats_later_standalone(monkeypatch):
    """组第 2 轮插队头不落后于其后到达的独立题（g1 两轮 + 独立题×2、
    limit=1 → [0, 2, 1, 3]：g1-t2 只让位于释放时已在队的 s0，先于 s1）。"""
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {"g1": 2}, standalone=2)
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        await asyncio.sleep(0)  # 模拟真实模型调用挂起（见上）
        calls.append(idx)
        return {
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"summary-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    await runner._run(task, cfg)

    assert calls == [0, 2, 1, 3]


async def test_session_fail_together_marks_remaining_rounds(monkeypatch):
    """组内任一轮失败：剩余轮次直接落「同组前序轮次失败」，不再调用模型。"""
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {"g1": 3})
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        raise ValueError("boom")

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    await runner._run(task, cfg)

    assert calls == [0]  # 第 1 轮失败后，第 2/3 轮不再请求模型
    assert len(task.results) == 3
    assert "ValueError" in task.results[0]["error"]
    assert "同组前序轮次失败" in task.results[1]["error"]
    assert "同组前序轮次失败" in task.results[2]["error"]
    assert task.done_total == 3


# ---------- 连坐失败（更新批） ----------

async def test_update_batch_fail_together(monkeypatch):
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {"g1": 2})
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        raise ValueError("boom")

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    batch = [(0, task.items[0]), (1, task.items[1])]
    await runner._run_update_batch_body(
        task, cfg, batch, options=task.options, manage_status=False
    )

    assert calls == [0]
    assert len(task.results) == 2
    assert "同组前序轮次失败" in task.results[1]["error"]


# ---------- 跨任务共享全局并发上限 ----------

async def test_global_limit_shared_across_tasks(monkeypatch):
    """两个任务同时评测，合计同时在评的题目数不超过全局上限（回归：并发翻倍）。"""
    cfg = load_config(Path("config"))
    task1 = _session_task(cfg, {}, standalone=4)
    task2 = _session_task(cfg, {}, standalone=4)
    active = 0
    peak = 0
    total_calls = 0

    async def fake_eval_one(mode, idx, item, **kw):
        nonlocal active, peak, total_calls
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        total_calls += 1
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    await asyncio.gather(runner._run(task1, cfg), runner._run(task2, cfg))

    assert total_calls == 8
    assert peak == 2  # 旧实现两任务各 4 并发，峰值会是 4+


# ---------- 两级限流：预处理不占模型槽 / 等待容量 ----------

def _prep_free_task(cfg, count: int = 2) -> Task:
    """无 frames 的 rich_content 独立题任务：one() 会走视频预处理分支。"""
    task = _session_task(cfg, {})
    task.items = [{"id": f"s{i}", "query": f"独立题{i}"} for i in range(count)]
    return task


def _patch_slow_prep(monkeypatch, events: list):
    """慢预处理（线程内 sleep）：经 to_thread 执行，可与模型调用并行。"""

    def fake_prep(item, **kw):
        events.append(("prep", item["id"]))
        time.sleep(0.01)
        return {**item, "frames": ["/tmp/fake/kf_001.jpg"], "frame_count": 1}

    monkeypatch.setattr(runner, "prepare_session_rich_content_item", fake_prep)


async def test_video_prep_runs_while_model_slots_busy(monkeypatch):
    """预处理不占模型槽：模型 limit=1 时第二题的预处理与第一题的模型调用并行。"""
    cfg = load_config(Path("config"))
    task = _prep_free_task(cfg)
    events: list = []

    async def fake_eval_one(mode, idx, item, **kw):
        events.append(("eval_start", idx))
        await asyncio.sleep(0.1)  # 留出窗口让第二题预处理并行进行
        events.append(("eval_end", idx))
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_slow_prep(monkeypatch, events)
    _patch_runner(
        monkeypatch, ResizableLimiter(1), fake_eval_one,
        pipeline_limiter=ResizableLimiter(2),
    )
    await runner._run(task, cfg)

    # 第二题预处理在第一题模型调用窗口内开始（没有等模型槽）
    assert events.index(("prep", "s1")) < events.index(("eval_end", 0))
    # 模型并发不超过 1：第二题的模型调用在第一题结束后才开始
    assert events.index(("eval_start", 1)) > events.index(("eval_end", 0))


async def test_waiting_capacity_bounds_pipeline(monkeypatch):
    """流水线准入 = 并发 + 等待容量：pipeline=1 时第二题在第一题释放
    准入槽之前不开始预处理（等待容量 0 的退化行为）。"""
    cfg = load_config(Path("config"))
    task = _prep_free_task(cfg)
    events: list = []

    async def fake_eval_one(mode, idx, item, **kw):
        events.append(("eval_start", idx))
        await asyncio.sleep(0.01)
        events.append(("eval_end", idx))
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_slow_prep(monkeypatch, events)
    _patch_runner(
        monkeypatch, ResizableLimiter(1), fake_eval_one,
        pipeline_limiter=ResizableLimiter(1),
    )
    await runner._run(task, cfg)

    # 第二题整体（含预处理）严格串行在第一题之后
    assert events == [
        ("prep", "s0"), ("eval_start", 0), ("eval_end", 0),
        ("prep", "s1"), ("eval_start", 1), ("eval_end", 1),
    ]


async def test_model_slot_wait_emits_progress_event(monkeypatch):
    """预处理完等模型槽时发逐题进度事件（percent=13「等待模型调用槽位」）。"""
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {}, standalone=2)  # 带 frames：跳过预处理直达模型槽
    release = asyncio.Event()

    async def fake_eval_one(mode, idx, item, **kw):
        if idx == 0:
            await release.wait()  # 第一题占住唯一模型槽
        return {"item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    run = asyncio.create_task(runner._run(task, cfg))
    await asyncio.sleep(0.05)  # 第二题进入模型槽等待

    progress1 = task.progress_events.get("1", [])
    assert any(
        e.get("percent") == 13 and "等待模型" in (e.get("message") or "")
        for e in progress1
    )

    release.set()
    await run
    assert len(task.results) == 2


async def test_update_batch_second_item_priority_requeue(monkeypatch):
    """更新批第 2 条插队头：批内第 2 条先于其后排队的独立题获得模型槽。
    两个任务各自从 0 编 index，故按 item id 记录顺序。"""
    cfg = load_config(Path("config"))
    batch_task = _session_task(cfg, {"g1": 2})
    other_task = _session_task(cfg, {}, standalone=2)
    calls: list[str] = []

    async def fake_eval_one(mode, idx, item, **kw):
        if item.get("id") == "g1_t1":
            await asyncio.sleep(0.05)  # 批第 1 条慢：留窗口让另一任务的题先排队
        await asyncio.sleep(0)  # 模拟真实模型调用挂起（见上）
        calls.append(item.get("id"))
        return {
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"summary-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    batch = [(0, batch_task.items[0]), (1, batch_task.items[1])]
    await asyncio.gather(
        runner._run_update_batch_body(
            batch_task, cfg, batch, options=batch_task.options
        ),
        runner._run(other_task, cfg),
    )

    # 批第 1 条先入；独立题 s0 在其模型调用期间排队、释放时被同步放行；
    # 批第 2 条插队头先于独立题 s1
    assert calls == ["g1_t1", "s0", "g1_t2", "s1"]


# ---------- 全局设置持久化 ----------

def test_settings_persist_roundtrip_and_limiter_align(tmp_path):
    path = tmp_path / "web_settings.json"
    try:
        scheduler.apply_settings(concurrency=3, eval_timeout_s=600)
        assert scheduler.MODEL_LIMITER.limit == 3
        assert scheduler.PIPELINE_LIMITER.limit == 3 + DEFAULT_WAITING_CAPACITY
        assert scheduler.persist_settings(path) is True

        scheduler.apply_settings(
            concurrency=DEFAULT_CONCURRENCY,
            eval_timeout_s=DEFAULT_EVAL_TIMEOUT_S,
        )
        scheduler.load_persisted_settings(path)
        assert scheduler.get_settings().concurrency == 3
        assert scheduler.get_settings().eval_timeout_s == 600.0
        assert scheduler.MODEL_LIMITER.limit == 3
        assert scheduler.PIPELINE_LIMITER.limit == 3 + DEFAULT_WAITING_CAPACITY

        # waiting_capacity 随设置往返，pipeline 上限随之重算
        scheduler.apply_settings(waiting_capacity=5)
        assert scheduler.PIPELINE_LIMITER.limit == 3 + 5
        assert scheduler.persist_settings(path) is True
        scheduler.apply_settings(waiting_capacity=DEFAULT_WAITING_CAPACITY)
        scheduler.load_persisted_settings(path)
        assert scheduler.get_settings().waiting_capacity == 5
        assert scheduler.PIPELINE_LIMITER.limit == 3 + 5

        # 仅调 concurrency：保留自定义 waiting_capacity 并重算 pipeline
        scheduler.apply_settings(concurrency=4)
        assert scheduler.get_settings().waiting_capacity == 5
        assert scheduler.MODEL_LIMITER.limit == 4
        assert scheduler.PIPELINE_LIMITER.limit == 4 + 5
    finally:
        _reset_scheduler_globals()


def test_settings_judges_persist_roundtrip_and_dedupe(tmp_path):
    """judges 随 web_settings.json 往返；apply 去重保序；坏结构回落空列表。"""
    import json

    path = tmp_path / "web_settings.json"
    try:
        scheduler.apply_settings(judges=["judge_2", "judge_2", "judge_1"])
        assert scheduler.get_settings().judges == ["judge_2", "judge_1"]
        assert scheduler.persist_settings(path) is True
        assert "judges" in json.loads(path.read_text(encoding="utf-8"))

        scheduler.apply_settings(judges=[])
        scheduler.load_persisted_settings(path)
        assert scheduler.get_settings().judges == ["judge_2", "judge_1"]

        # judges 键结构非法（非字符串元素）：忽略，不改现有值
        path.write_text(
            json.dumps({"judges": ["judge_2", 3]}), encoding="utf-8"
        )
        scheduler.load_persisted_settings(path)
        assert scheduler.get_settings().judges == ["judge_2", "judge_1"]
    finally:
        _reset_scheduler_globals()


def test_load_persisted_settings_bad_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not-json", encoding="utf-8")
    try:
        settings = scheduler.load_persisted_settings(path)
        assert settings.concurrency == DEFAULT_CONCURRENCY
        assert settings.waiting_capacity == DEFAULT_WAITING_CAPACITY
        assert settings.eval_timeout_s == DEFAULT_EVAL_TIMEOUT_S
        assert settings.judges == []
    finally:
        _reset_scheduler_globals()


def test_apply_settings_clamps_out_of_range():
    try:
        scheduler.apply_settings(concurrency=9999, waiting_capacity=-5, eval_timeout_s=1)
        assert scheduler.get_settings().concurrency == scheduler.MAX_CONCURRENCY
        assert scheduler.get_settings().waiting_capacity == scheduler.MIN_WAITING_CAPACITY
        assert scheduler.get_settings().eval_timeout_s == scheduler.MIN_EVAL_TIMEOUT_S
        assert scheduler.PIPELINE_LIMITER.limit == (
            scheduler.MAX_CONCURRENCY + scheduler.MIN_WAITING_CAPACITY
        )
    finally:
        _reset_scheduler_globals()


def test_settings_panel_static_asserts():
    """前端设置面板接线静态断言：等待容量输入 + 两级队列计数展示。"""
    project_root = Path(__file__).resolve().parents[1]
    app_js = (project_root / "src/auto_eval/web/static/app.js").read_text(
        encoding="utf-8"
    )
    index_html = (project_root / "src/auto_eval/web/static/index.html").read_text(
        encoding="utf-8"
    )
    assert "settingsForm.value.waiting_capacity" in app_js
    assert "waiting_capacity: waitingCapacity" in app_js
    assert "sysSettings.waiting_capacity" in index_html
    assert "sysSettings.pipeline.running" in index_html


# ---------- 设置 API ----------

def test_settings_api_roundtrip_and_validation(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from auto_eval.web.server import app

    monkeypatch.setattr(scheduler, "SETTINGS_PATH", tmp_path / "web_settings.json")
    try:
        with TestClient(app) as client:
            body = client.get("/api/settings").json()
            assert body["concurrency"] == DEFAULT_CONCURRENCY
            assert body["waiting_capacity"] == DEFAULT_WAITING_CAPACITY
            assert "queue" in body and {"limit", "running", "queued"} <= set(body["queue"])
            assert "pipeline" in body and {"limit", "running", "queued"} <= set(
                body["pipeline"]
            )
            assert body["pipeline"]["limit"] == (
                DEFAULT_CONCURRENCY + DEFAULT_WAITING_CAPACITY
            )

            assert client.put("/api/settings", json={}).status_code == 422
            assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
            assert client.put("/api/settings", json={"eval_timeout_s": 5}).status_code == 422
            assert (
                client.put("/api/settings", json={"waiting_capacity": -1}).status_code
                == 422
            )
            assert (
                client.put("/api/settings", json={"waiting_capacity": 999}).status_code
                == 422
            )

            response = client.put(
                "/api/settings", json={"concurrency": 5, "eval_timeout_s": 480}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["concurrency"] == 5
            assert body["eval_timeout_s"] == 480
            assert body["persisted"] is True
            assert (tmp_path / "web_settings.json").exists()

            assert scheduler.MODEL_LIMITER.limit == 5
            assert scheduler.PIPELINE_LIMITER.limit == 5 + DEFAULT_WAITING_CAPACITY
            assert scheduler.get_settings().eval_timeout_s == 480

            # 仅传 waiting_capacity 也可保存，pipeline 随之和重算
            response = client.put("/api/settings", json={"waiting_capacity": 3})
            assert response.status_code == 200
            assert response.json()["waiting_capacity"] == 3
            assert scheduler.PIPELINE_LIMITER.limit == 5 + 3

            # judges：未知裁判名 / 空列表 → 422；仅传 judges 也可保存
            known = [j.name for j in load_config(Path("config")).judges]
            assert (
                client.put(
                    "/api/settings", json={"judges": ["no_such_judge"]}
                ).status_code
                == 422
            )
            assert (
                client.put("/api/settings", json={"judges": []}).status_code == 422
            )
            response = client.put("/api/settings", json={"judges": known})
            assert response.status_code == 200
            assert response.json()["judges"] == known
            assert scheduler.get_settings().judges == known
            # GET 返回过滤后的裁判；未设置时回落第一个配置裁判
            assert client.get("/api/settings").json()["judges"] == known
    finally:
        _reset_scheduler_globals()
