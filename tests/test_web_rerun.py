"""手动重跑：选择驱动切片、context 剥离、种子总结链、状态 heal、结果按输入顺序、API。"""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from auto_eval.config import load_config
from auto_eval.web import runner, server
from auto_eval.web.history import snapshot_payload
from auto_eval.web.scheduler import ResizableLimiter
from auto_eval.web.tasks import TASKS, Task, upsert_result_by_index

from test_web_scheduler import _patch_runner, _reset_scheduler_globals

_CFG = load_config(Path("config"))


def _task(groups: dict[str, int], *, standalone: int = 0, status: str = "done") -> Task:
    """构造带污染 context + 运行时键的 rich_content 任务（模拟跑过一轮后的状态）。"""
    items = []
    for grp, turns in groups.items():
        for turn in range(1, turns + 1):
            items.append({
                "id": f"{grp}_t{turn}",
                "query": f"{grp} 第{turn}轮",
                "context": f"{grp} 原始背景{turn}\n\n历史对话总结：\n【第1轮】上次运行的总结\n",
                "frames": [f"/tmp/{grp}_{turn}/kf_001.jpg"],
                "video_name": f"{grp}_{turn}.mp4",
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
        id="rerun-test",
        mode="rich_content",
        items=items,
        options={"judges": [_CFG.judges[0].name]},
        status=status,
    )


def _result(idx: int, item: dict, *, error: str | None = None, turn_summary: str | None = None) -> dict:
    res = {"index": idx, "item_id": item.get("id"), "query": item.get("query")}
    if error:
        res["error"] = error
    if turn_summary is not None:
        res["turn_summary"] = turn_summary
    return res


def _drain_events(q: asyncio.Queue) -> list[dict]:
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


def _patch_video_prep(monkeypatch):
    """重跑副本按设计剔除 frames 强制重抽帧；测试环境无真实视频，patch 掉抽帧。"""

    def fake_prep(item, **kw):
        return {
            **item,
            "video_path": item.get("video_path") or "/tmp/fake.mp4",
            "video_name": "fake.mp4",
            "media": ["/tmp/fake.mp4"],
            "frames": ["/tmp/fake/kf_001.jpg"],
            "frame_count": 1,
            "duration": 1.0,
        }

    monkeypatch.setattr(runner, "prepare_session_rich_content_item", fake_prep)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", fake_prep)


# ---------- 纯函数：_strip_injected_summary ----------

def test_strip_injected_summary_both_marker_forms():
    assert runner._strip_injected_summary("原始背景\n\n历史对话总结：\n【第1轮】s\n") == "原始背景"
    assert runner._strip_injected_summary("历史对话总结：\n【第1轮】s\n") == ""
    # 干净 / 空 context 原样返回；多次注入只切第一次（幂等）
    assert runner._strip_injected_summary("干净的背景") == "干净的背景"
    assert runner._strip_injected_summary("") == ""
    twice = "原始\n\n历史对话总结：\n【第1轮】a\n\n历史对话总结：\n【第1轮】b\n"
    assert runner._strip_injected_summary(twice) == "原始"


# ---------- build_rerun_batches（选择驱动） ----------

def test_build_rerun_batches_slices_from_first_selected():
    task = _task({"g1": 3})
    task.results = [
        _result(0, task.items[0], turn_summary="s1"),
        _result(1, task.items[1], error="ValueError: boom"),
        _result(2, task.items[2], error="同组前序轮次失败：ValueError: boom"),
    ]
    batches = runner.build_rerun_batches(task, [1])
    assert len(batches) == 1
    rb = batches[0]
    assert [i for i, _ in rb.batch] == [1, 2]  # 从首个选中轮切到组尾
    assert rb.initial_summary == "【第1轮】s1\n"  # 前缀轮总结复用
    assert rb.initial_turn == 1
    assert rb.group == "g1"
    # 副本：剥离上次注入的总结块 + 剔除运行时键，且不是 task.items 本体
    for i, copy in rb.batch:
        assert copy["context"] == f"g1 原始背景{i + 1}"
        assert "frames" not in copy
        assert "video_name" not in copy
        assert copy is not task.items[i]


def test_build_rerun_batches_good_items_rerunnable():
    """选择驱动：成功条目同样可重跑，不再按健康度 skip。"""
    task = _task({"g1": 2}, standalone=1)
    task.results = [
        _result(0, task.items[0], turn_summary="s1"),
        _result(1, task.items[1], turn_summary="s2"),
        _result(2, task.items[2], turn_summary="s3"),
    ]
    # 全好组选首轮 → 从首选中轮切到组尾
    batches = runner.build_rerun_batches(task, [0])
    assert [b.group for b in batches] == ["g1"]
    assert [i for i, _ in batches[0].batch] == [0, 1]
    assert batches[0].initial_summary == ""
    assert batches[0].initial_turn == 0
    # 好独立题 → 单题批
    batches = runner.build_rerun_batches(task, [2])
    assert [b.group for b in batches] == ["standalone:2"]
    assert [i for i, _ in batches[0].batch] == [2]


def test_build_rerun_batches_selection_not_health():
    """未评估条目可选；选中哪轮就从哪轮切，不由健康度推断。"""
    task = _task({"g1": 3})
    task.results = [_result(0, task.items[0], turn_summary="s1")]  # 第2/3轮从未评估
    # 只选第3轮 → 仅重跑第3轮，前缀（含未评估的第2轮）回落（未生成总结）
    batches = runner.build_rerun_batches(task, [2])
    assert [i for i, _ in batches[0].batch] == [2]
    assert batches[0].initial_summary == "【第1轮】s1\n【第2轮】（未生成总结）\n"
    assert batches[0].initial_turn == 2
    # 前端「全选失败」会展开为从首个坏轮（第2轮）起
    batches = runner.build_rerun_batches(task, [1, 2])
    assert [i for i, _ in batches[0].batch] == [1, 2]
    assert batches[0].initial_turn == 1


def test_build_rerun_batches_latest_result_wins():
    """前缀总结取同 index 最新结果（后写赢）的 turn_summary。"""
    task = _task({"g1": 2})
    # 旧坏新好：前缀用最新好结果的总结
    task.results = [
        _result(0, task.items[0], error="boom"),
        _result(1, task.items[1], error="同组前序轮次失败：boom"),
        _result(0, task.items[0], turn_summary="s1-recovered"),
    ]
    batches = runner.build_rerun_batches(task, [1])
    assert [i for i, _ in batches[0].batch] == [1]
    assert batches[0].initial_summary == "【第1轮】s1-recovered\n"

    # 旧好新坏：最新是 error 行（无 turn_summary）→ 回落（未生成总结）
    task.results = [
        _result(0, task.items[0], turn_summary="s1"),
        _result(0, task.items[0], error="RateLimitError: 限流"),
    ]
    batches = runner.build_rerun_batches(task, [1])
    assert batches[0].initial_summary == "【第1轮】（未生成总结）\n"


def test_build_rerun_batches_standalone_and_summary_fallback():
    task = _task({"g1": 2}, standalone=1)
    task.results = [
        _result(0, task.items[0]),  # 好结果但缺 turn_summary → （未生成总结）
    ]
    batches = runner.build_rerun_batches(task, [1, 2])
    by_group = {rb.group: rb for rb in batches}
    assert set(by_group) == {"g1", "standalone:2"}
    assert by_group["g1"].initial_summary == "【第1轮】（未生成总结）\n"
    assert by_group["standalone:2"].initial_summary == ""
    assert by_group["standalone:2"].initial_turn == 0
    assert [i for i, _ in by_group["standalone:2"].batch] == [2]


async def test_update_batch_initial_summary_and_turn_offset(monkeypatch):
    """种子总结链 + 轮次偏移：重跑批从第2轮起编号，不出现重复【第1轮】。"""
    task = _task({"g1": 3})
    task.results = [_result(0, task.items[0], turn_summary="s1")]
    seen_contexts: list[str] = []

    async def fake_eval_one(mode, idx, item, **kw):
        seen_contexts.append(item.get("context") or "")
        return {
            "index": idx,
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"new-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    _patch_video_prep(monkeypatch)
    batches = runner.build_rerun_batches(task, [1])
    rb = batches[0]
    await runner._run_update_batch_body(
        task, _CFG, rb.batch, options=task.options,
        manage_status=False, initial_summary=rb.initial_summary,
        initial_turn=rb.initial_turn,
    )

    # 第2轮拿到种子链（复用第1轮旧总结），第3轮的链是 【第1轮】s1 + 【第2轮】new-1
    assert "【第1轮】s1" in seen_contexts[0]
    assert "【第1轮】s1" in seen_contexts[1]
    assert "【第2轮】new-1" in seen_contexts[1]
    # 第1轮旧结果保留，第2/3轮按 index 追加且有序
    assert [r["index"] for r in task.results] == [0, 1, 2]
    assert task.results[0]["turn_summary"] == "s1"


async def test_update_batch_default_args_unchanged(monkeypatch):
    """不传 initial_summary/initial_turn 的既有调用路径行为不变（回归保护）。"""
    task = _task({"g1": 2})
    contexts: list[str] = []

    async def fake_eval_one(mode, idx, item, **kw):
        contexts.append(item.get("context") or "")
        return {"item_id": item.get("id"), "query": item.get("query"), "turn_summary": f"s{idx}"}

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    _patch_video_prep(monkeypatch)
    batch = [(0, {"id": "g1_t1", "query": "q1"}), (1, {"id": "g1_t2", "query": "q2"})]
    await runner._run_update_batch_body(task, _CFG, batch, options=task.options)
    assert contexts[0] == ""  # 无种子
    assert "【第1轮】s0" in contexts[1]  # 编号从 1 开始


async def test_heal_only_when_all_items_healthy(monkeypatch):
    """部分修复不 heal（R4 补发 error）；全部修复才 heal 为 done 并补发 done。"""
    task = _task({"g1": 2}, status="error")
    task.error = "服务中断"
    task.results = [_result(0, task.items[0], error="boom"), _result(1, task.items[1], error="boom")]

    async def fake_eval_one(mode, idx, item, **kw):
        return {"index": idx, "item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    monkeypatch.setattr(runner, "save_task", lambda task: None)  # 保留 _flush_now 的 summary 重算

    # 只修 idx 1：idx 0 仍坏 → 不 heal，R4 补发 error
    q1 = task.subscribe()
    await runner.run_update_batch(
        task, _CFG, [(1, dict(task.items[1]))], options=task.options, manage_status=False
    )
    assert task.status == "error"
    events1 = _drain_events(q1)
    assert events1[-1]["event"] == "error"
    task.unsubscribe(q1)

    # 修复剩下的 idx 0 → 全部健康 → heal + done 终态
    q2 = task.subscribe()
    await runner.run_update_batch(
        task, _CFG, [(0, dict(task.items[0]))], options=task.options, manage_status=False
    )
    assert task.status == "done"
    assert task.error is None
    events2 = _drain_events(q2)
    assert events2[-1]["event"] == "done"
    assert events2[-1]["data"]["total"] == 2
    assert isinstance(events2[-1]["data"]["summary"], dict)


# ---------- 结果按输入顺序（index 升序单行） ----------

def test_upsert_result_by_index_orders_and_dedupes():
    task = _task({"g1": 1}, standalone=3)
    # 乱序完成 → 按 index 升序落位
    for idx in (3, 1, 0, 2):
        assert upsert_result_by_index(task, _result(idx, task.items[idx])) == "appended"
    assert [r["index"] for r in task.results] == [0, 1, 2, 3]
    # 同 index 重评 → 原位替换（单行，不追加）
    assert upsert_result_by_index(task, _result(1, task.items[1], turn_summary="new")) == "replaced"
    assert [r["index"] for r in task.results] == [0, 1, 2, 3]
    assert task.results[1]["turn_summary"] == "new"
    # legacy 重复行：替换最后一条（与最新结果读取口径一致）
    task.results.append(_result(0, task.items[0], turn_summary="dupe"))
    upsert_result_by_index(task, _result(0, task.items[0], turn_summary="fixed"))
    zeros = [r for r in task.results if r["index"] == 0]
    assert [r.get("turn_summary") for r in zeros] == [None, "fixed"]


async def test_run_update_batch_writes_sorted_results(monkeypatch):
    """批内乱序 index 依次完成，task.results 仍按输入顺序排列。"""
    task = _task({"g1": 1}, standalone=2)

    async def fake_eval_one(mode, idx, item, **kw):
        return {"index": idx, "item_id": item.get("id"), "query": item.get("query")}

    _patch_runner(monkeypatch, ResizableLimiter(2), fake_eval_one)
    _patch_video_prep(monkeypatch)
    await runner._run_update_batch_body(
        task, _CFG,
        [(2, dict(task.items[2])), (0, dict(task.items[0])), (1, dict(task.items[1]))],
        options=task.options,
    )
    assert [r["index"] for r in task.results] == [0, 1, 2]


def test_snapshot_payload_sorts_legacy_results():
    """旧快照（完成顺序）经历史详情读出时按 index 升序；无 index 行排末尾。"""
    payload = snapshot_payload({
        "task_id": "t",
        "results": [
            {"index": 2, "query": "c"},
            {"index": 0, "query": "a"},
            {"query": "no-index"},
            {"index": 1, "query": "b"},
        ],
    })
    assert [r.get("index") for r in payload["results"]] == [0, 1, 2, None]


# ---------- POST /api/eval/rerun ----------

def test_rerun_endpoint_validation(monkeypatch):
    task = _task({"g1": 2})
    task.results = [_result(0, task.items[0], error="boom"), _result(1, task.items[1], error="x")]
    TASKS[task.id] = task
    try:
        with TestClient(server.app) as client:
            assert client.post(
                "/api/eval/rerun", json={"task_id": "no-such-task", "indexes": [0]}
            ).status_code == 404
            assert client.post(
                "/api/eval/rerun", json={"task_id": task.id, "indexes": []}
            ).status_code == 400
            assert client.post(
                "/api/eval/rerun", json={"task_id": task.id, "indexes": [99]}
            ).status_code == 422
            task.active_runs = 1
            assert client.post(
                "/api/eval/rerun", json={"task_id": task.id, "indexes": [0]}
            ).status_code == 409
            task.active_runs = 0
    finally:
        TASKS.pop(task.id, None)
        _reset_scheduler_globals()


async def test_rerun_endpoint_end_to_end(monkeypatch):
    """选中条目 → 服务端组展开切片 → 后台批按 index upsert，结果按输入顺序。"""
    task = _task({"g1": 2}, standalone=1)
    task.results = [
        _result(0, task.items[0], turn_summary="s1"),
        _result(1, task.items[1], error="ValueError: boom"),
        # 独立题 idx 2 从未评估（中断任务）
    ]
    calls: list[int] = []

    async def fake_eval_one(mode, idx, item, **kw):
        calls.append(idx)
        return {
            "index": idx,
            "item_id": item.get("id"),
            "query": item.get("query"),
            "turn_summary": f"rerun-{idx}",
        }

    _patch_runner(monkeypatch, ResizableLimiter(4), fake_eval_one)
    _patch_video_prep(monkeypatch)
    TASKS[task.id] = task
    old_cfg = server._state.get("cfg")
    server._state["cfg"] = _CFG
    try:
        resp = await server.api_eval_rerun(
            server.RerunReq(task_id=task.id, indexes=[1, 2])
        )
        assert resp["task_id"] == task.id
        assert resp["rerun_indexes"] == [1, 2]
        assert "skipped_groups" not in resp
        groups = {b["group"]: b for b in resp["batches"]}
        assert groups["g1"]["indexes"] == [1]
        assert groups["g1"]["initial_turn"] == 1
        assert groups["standalone:2"]["indexes"] == [2]
        # 等待 spawn_background 的批完成
        await asyncio.gather(*list(runner._BACKGROUND_TASKS))
        assert sorted(calls) == [1, 2]
        # 旧 error 行按 index 原位覆盖，未评估条目拿到结果，整体保持输入顺序
        assert [r["index"] for r in task.results] == [0, 1, 2]
        assert task.results[1]["turn_summary"] == "rerun-1"
        assert task.results[2]["item_id"] == "s0"
        assert task.active_runs == 0
        assert task.status == "done"
    finally:
        TASKS.pop(task.id, None)
        server._state["cfg"] = old_cfg
        _reset_scheduler_globals()
