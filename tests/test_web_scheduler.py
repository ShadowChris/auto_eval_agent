"""全局评测队列：可调限流器、组内连续执行、连坐失败、全局设置。"""
import asyncio
import contextlib
from pathlib import Path

from auto_eval.config import load_config
from auto_eval.web import runner, scheduler
from auto_eval.web.scheduler import DEFAULT_CONCURRENCY, DEFAULT_EVAL_TIMEOUT_S, ResizableLimiter
from auto_eval.web.tasks import Task


def _reset_scheduler_globals():
    scheduler.apply_settings(
        concurrency=DEFAULT_CONCURRENCY,
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


def _patch_runner(monkeypatch, limiter: ResizableLimiter, fake_eval_one):
    monkeypatch.setattr(runner, "EVAL_LIMITER", limiter)
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


# ---------- 组连续 + 连坐失败（全量跑批） ----------

async def test_session_rounds_run_back_to_back(monkeypatch):
    """limit=1 时同组各轮背靠背连续执行，不再被排到其他组之后（回归：第 2 轮排队尾）。"""
    cfg = load_config(Path("config"))
    task = _session_task(cfg, {"g1": 3, "g2": 3})
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        return {
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"summary-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(1), fake_eval_one)
    await runner._run(task, cfg)

    assert calls == [0, 1, 2, 3, 4, 5]  # g1 三轮连续完成，才轮到 g2
    assert "【第1轮】summary-0" in task.items[1]["context"]
    assert "【第2轮】summary-1" in task.items[2]["context"]


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


# ---------- 全局设置持久化 ----------

def test_settings_persist_roundtrip_and_limiter_align(tmp_path):
    path = tmp_path / "web_settings.json"
    try:
        scheduler.apply_settings(concurrency=3, eval_timeout_s=600)
        assert scheduler.EVAL_LIMITER.limit == 3
        assert scheduler.persist_settings(path) is True

        scheduler.apply_settings(
            concurrency=DEFAULT_CONCURRENCY, eval_timeout_s=DEFAULT_EVAL_TIMEOUT_S
        )
        scheduler.load_persisted_settings(path)
        assert scheduler.get_settings().concurrency == 3
        assert scheduler.get_settings().eval_timeout_s == 600.0
        assert scheduler.EVAL_LIMITER.limit == 3
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
        assert settings.eval_timeout_s == DEFAULT_EVAL_TIMEOUT_S
        assert settings.judges == []
    finally:
        _reset_scheduler_globals()


def test_apply_settings_clamps_out_of_range():
    try:
        scheduler.apply_settings(concurrency=9999, eval_timeout_s=1)
        assert scheduler.get_settings().concurrency == scheduler.MAX_CONCURRENCY
        assert scheduler.get_settings().eval_timeout_s == scheduler.MIN_EVAL_TIMEOUT_S
    finally:
        _reset_scheduler_globals()


# ---------- 设置 API ----------

def test_settings_api_roundtrip_and_validation(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from auto_eval.web.server import app

    monkeypatch.setattr(scheduler, "SETTINGS_PATH", tmp_path / "web_settings.json")
    try:
        with TestClient(app) as client:
            body = client.get("/api/settings").json()
            assert body["concurrency"] == DEFAULT_CONCURRENCY
            assert "queue" in body and {"limit", "running", "queued"} <= set(body["queue"])

            assert client.put("/api/settings", json={}).status_code == 422
            assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
            assert client.put("/api/settings", json={"eval_timeout_s": 5}).status_code == 422

            response = client.put(
                "/api/settings", json={"concurrency": 5, "eval_timeout_s": 480}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["concurrency"] == 5
            assert body["eval_timeout_s"] == 480
            assert body["persisted"] is True
            assert (tmp_path / "web_settings.json").exists()

            assert scheduler.EVAL_LIMITER.limit == 5
            assert scheduler.get_settings().eval_timeout_s == 480

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
