"""评估执行：垂域视觉评测 / 垂域视觉对比评测 + 两级并发 + 推 SSE 事件 + 汇总。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..paths import RUNS_DIR
from ..config import AppConfig
from ..judges import (
    JudgeClient,
    RichContentJudge,
    VisualCompareJudge,
)
from ..judges.base import flush_web_trace_records
from ..llm_stream import is_retriable_llm_error
from ..observability import (
    bind_chain_context,
    error_details,
    log_event,
    make_request_id,
)
from ..schema import EvalItem
from .history import save_task
from .video_prepare import (
    prepare_session_rich_content_item,
    prepare_session_visual_compare_item,
)
from .scheduler import MODEL_LIMITER, PIPELINE_LIMITER, get_settings
from .tasks import Task, retire_task, upsert_result_by_index


logger = logging.getLogger(__name__)
MAX_PROGRESS_EVENTS_PER_ITEM = 100


# 持久化节流：普通调用走 debounce（默认 2s，环境变量可调）或每 N 题强刷一次，
# 避免大任务每完成一题就把 items+全部 results+progress_events 全量 json.dumps
# （O(n²) 序列化、瞬时内存峰值约快照大小的 2-3 倍）。force=True 立即落盘，
# 用于终态/异常/退休前。语义变化：进程崩溃最多丢 debounce 窗口内的结果；
# 任务正常终态保证盘上完整（eval_errors.jsonl 与 judge trace 仍即时写）。
_PERSIST_DEBOUNCE_S = float(os.environ.get("AUTO_EVAL_PERSIST_DEBOUNCE_S", "2.0"))
_PERSIST_FORCE_EVERY_N = 20
_pending_flush: dict[str, asyncio.TimerHandle] = {}
_unpersisted: dict[str, int] = {}


def _flush_now(task: Task) -> None:
    """立即落盘：取消 pending 定时器、重算 summary、save_task。"""
    handle = _pending_flush.pop(task.id, None)
    if handle is not None:
        handle.cancel()
    _unpersisted.pop(task.id, None)
    task.summary = _summarize(task)
    try:
        save_task(task)
    except Exception:
        logger.exception("unexpected task snapshot failure: task_id=%s", task.id)


def _persist_task(task: Task, *, force: bool = False) -> None:
    """Persist without allowing history I/O to break the evaluation/SSE.

    TimerHandle 闭包直接持 task 引用（不按 id 回查 TASKS）：退休前必先
    force 强刷，届时 pending 定时器已被取消，不存在退休后再刷盘的窗口。
    """
    if force:
        _flush_now(task)
        return
    n = _unpersisted.get(task.id, 0) + 1
    _unpersisted[task.id] = n
    if task.id in _pending_flush:
        return  # 已有定时刷在排队，等它
    if n >= _PERSIST_FORCE_EVERY_N:
        _flush_now(task)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _flush_now(task)  # 无事件循环的同步上下文，直接刷
        return
    _pending_flush[task.id] = loop.call_later(
        _PERSIST_DEBOUNCE_S,
        lambda t=task: _flush_now(t),
    )


def _record_progress(task: Task, item_index: int, payload: dict) -> dict:
    """Store one bounded Web projection of the same structured log event."""
    key = str(item_index)
    events = task.progress_events.setdefault(key, [])
    sequence = int(events[-1].get("sequence", 0)) + 1 if events else 1
    event_payload = {**payload, "sequence": sequence}
    previous = task.item_progress.get(key) or {}
    if "started_at" not in event_payload and previous.get("started_at") is not None:
        event_payload["started_at"] = previous["started_at"]
    events.append(event_payload)
    if len(events) > MAX_PROGRESS_EVENTS_PER_ITEM:
        del events[:-MAX_PROGRESS_EVENTS_PER_ITEM]
    task.item_progress[key] = event_payload
    task._fanout("item_progress", event_payload)
    return event_payload


def reset_item_progress(task: Task, indexes: list[int]) -> None:
    """重跑前重置所选条目的逐题进度：清掉上一轮的事件序列与终态
    （done/error、percent 100），回落「排队中（重跑）」并即时 fanout——
    已连接的 SSE 订阅者立刻看到这些行重新排队，之后连接的回放到的
    也是 pending 态。须在 spawn 重跑批次之前同步调用。
    """
    for idx in indexes:
        if not isinstance(idx, int) or not (0 <= idx < len(task.items)):
            continue
        task.progress_events.pop(str(idx), None)
        item = task.items[idx]
        _record_progress(
            task,
            idx,
            {
                "item_index": idx,
                "item_id": item.get("id", f"q{idx}"),
                "status": "pending",
                "percent": 0,
                "message": "排队中（重跑）",
                "module": "任务",
                "event": "重跑排队",
                "level": "info",
                "updated_at": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
            },
        )


def _to_evalitem(item: dict, idx: int) -> EvalItem:
    meta = dict(item.get("metadata") or {})
    if item.get("frames"):
        meta["frames"] = item["frames"]  # 抽好的关键帧路径，裁判读取后 encode 成 image_url
    return EvalItem(
        id=item.get("id", f"q{idx}"),
        question=item["query"],
        context=item.get("context"),
        category=item.get("category", "default"),
        media=item.get("media") or [],
        metadata=meta,
    )


def _mark_interrupted_if_stuck(task: Task) -> bool:
    """R2：取消/未捕获 BaseException 会跳过 `except Exception`，任务可能停在
    pending/running——retire/_enforce_capacity 均拒绝非终态，Task 将永久 pin
    （不可删、DELETE 永久 409）。置 error 并通知订阅者后返回 True。"""
    if task.status not in {"pending", "running"}:
        return False
    task.status = "error"
    task.error = task.error or "服务中断，已保留中断前完成的评估结果"
    task._fanout("error", {"message": task.error})
    return True


async def run_eval(task: Task, cfg: AppConfig) -> None:
    """全量评测入口。pin 契约（R1）：调用方须在提交时同步 `active_runs += 1`
    （endpoint 在 spawn_background 之前），本函数只负责结束时解除——否则
    spawn 延迟窗口内任务可被 DELETE/LRU 淘汰，引发快照复活或双对象覆盖。"""
    try:
        await task.publish("start", {"total": len(task.items), "mode": task.mode})
        task.status = "running"
        _persist_task(task, force=True)
        try:
            await _run(task, cfg)
            task.summary = _summarize(task)
            task.status = "done"
            await task.publish("done", {"summary": task.summary, "total": len(task.items)})
            _persist_task(task, force=True)
        except Exception as e:
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            await task.publish("error", {"message": task.error})
            _persist_task(task, force=True)
    finally:
        task.active_runs -= 1
        _mark_interrupted_if_stuck(task)
        _persist_task(task, force=True)  # 退休前最后一次落盘，磁盘先于内存下线
        retire_task(task)


def _make_item_evaluator(
    task: Task,
    cfg: AppConfig,
    *,
    options: dict | None = None,
    on_result: Callable[[int, dict, float], Awaitable[None]] | None = None,
) -> tuple[
    Callable[..., Awaitable[dict]],
    Callable[[int, dict, str], Awaitable[dict]],
    list[JudgeClient],
]:
    """构造单题评测协程 one(idx, item_dict, *, priority=False) -> res（含失败
    res，不抛出）、连坐失败协程 fail(idx, item_dict, reason) -> res，以及本套
    裁判客户端（调用方负责在 finally 中 aclose，见 _aclose_judge_clients）。

    两级并发槽：调用方（_run / _run_update_batch_body）须已通过
    PIPELINE_LIMITER 取得流水线准入槽（界定预处理/等待中的评测总量）；
    one 内部在进入模型调用阶段前自行获取 MODEL_LIMITER 模型槽——视频
    预处理不占模型槽，模型槽满时预处理照常进行。priority=True（组第 2+
    轮 / 批第 2+ 条）请求模型槽时插队头，保留组内轮次不被排到队尾的语义。
    流水线准入排队不计入单题耗时；模型槽等待计入（该题占着准入名额），
    纯模型时长另有 latency_s 口径。并发、等待容量、单题超时与裁判由全局
    设置管理（scheduler.get_settings），不再从 options 读取（旧 options 中
    的 concurrency/eval_timeout_s 已废弃；options.judges 仍生效但仅作兼容
    优先，前端已改为全局设置选裁判）。
    options：本次运行生效的配置（缺省 task.options），只读、不回写，
    供更新批以 {**task.options, **req.options} 运行。
    on_result：结果落地回调 (idx, res, started)，在评测上下文（bind_chain_context
    内）被 await，负责 append/merge、完成日志、SSE、持久化；缺省为完整跑批的原有行为。
    """
    runtime_options = options if options is not None else task.options
    # 裁判优先级：任务 options（旧快照/脚本兼容）→ 全局设置 → 配置第一个
    selected = (
        runtime_options.get("judges")
        or get_settings().judges
        or [cfg.judges[0].name]
    )
    judges_cfg = [j for j in cfg.judges if j.name in selected] or cfg.judges[:1]
    # R3：构造中途失败（如某个 judge 缺 base_url）时，已建客户端的连接池会
    # 无人关闭而泄漏——先登记再逐个构造，失败时交后台任务关闭后重抛。
    clients: list[JudgeClient] = []
    try:
        for j in judges_cfg:
            clients.append(JudgeClient(j))
    except BaseException:
        if clients:
            spawn_background(_aclose_judge_clients(clients))
        raise
    rich_profile = cfg.visual_modes.get("rich_content")
    rich_judges = (
        [RichContentJudge(client, rich_profile) for client in clients]
        if rich_profile is not None
        else []
    )
    # 垂域视觉对比：双视频多模态对比裁判（复用同一视觉配置）
    compare_judges = (
        [VisualCompareJudge(client, rich_profile) for client in clients]
        if rich_profile is not None
        else []
    )
    # 垂域→中文显示名映射（rich_content.yaml 的 category_display）
    category_display = rich_profile.category_display if rich_profile else {}
    loop = asyncio.get_running_loop()

    async def _default_on_result(idx: int, res: dict, started: float) -> None:
        upsert_result_by_index(task, res)  # 按 index 有序落位，结果表保持输入顺序
        task.done_total += 1
        failed = bool(res.get("error"))
        log_event(
            "任务",
            "完成",
            level=logging.ERROR if failed else logging.INFO,
            details={
                "状态": "失败" if failed else "成功",
                "总耗时": f"{time.perf_counter() - started:.2f}秒",
                "错误": res.get("error"),
            },
            progress=100,
            progress_message="评测失败" if failed else "评测完成",
            progress_status="error" if failed else "done",
        )
        await task.publish(
            "result",
            {"progress": task.done_total, "total": len(task.items), "result": res},
        )
        _persist_task(task)

    finish = on_result or _default_on_result

    def _progress_publisher(idx: int) -> Callable[[dict], None]:
        def publish_progress(payload: dict) -> None:
            def apply() -> None:
                _record_progress(task, idx, payload)
            try:
                if asyncio.get_running_loop() is loop:
                    apply()
                else:
                    loop.call_soon_threadsafe(apply)
            except RuntimeError:
                loop.call_soon_threadsafe(apply)
        return publish_progress

    async def one(idx: int, item_dict: dict, *, priority: bool = False) -> dict:
        # 调用方须已取得流水线准入槽（见 _run / _run_update_batch_body）；
        # 计时从进入本函数起：流水线排队不计入，模型槽等待计入单题耗时
        # （纯模型时长另有 latency_s 口径）。priority=True 时模型槽插队头
        # （组第 2+ 轮 / 批第 2+ 条优先续队，不排到新到达者之后）。
        request_id = make_request_id(task.created_at, task.id, idx)
        pending_judge_traces: list[tuple[str, dict]] = []
        publish_progress = _progress_publisher(idx)

        item_id = item_dict.get("id") or f"q{idx}"

        def collect_judge_trace(trace_path: str, record: dict) -> None:
            pending_judge_traces.append((trace_path, record))

        with bind_chain_context(
            task_id=task.id,
            session_name=task.session_name,
            request_id=request_id,
            item_id=item_id,
            item_index=idx,
            progress_callback=publish_progress,
            judge_trace_callback=collect_judge_trace,
        ):
            log_event(
                "任务",
                "开始",
                details={
                    "问题": item_dict.get("query", ""),
                    "模式": task.mode,
                    "裁判": ",".join(j.display or j.name for j in judges_cfg),
                },
                progress=0,
                progress_message="排队等待评测",
            )
            started = time.perf_counter()
            log_event(
                "任务",
                "开始评测",
                progress=1,
                progress_message="开始评测",
                progress_fields={"started_at": int(time.time() * 1000)},
            )
            last_error = None
            res = None
            if not item_dict.get("frames") and not item_dict.get("frames1"):
                try:
                    log_event(
                        "视频准备",
                        "校验视频并分析场景",
                        details={"视频路径": item_dict.get("video_path")},
                        progress=3,
                        progress_message="正在校验视频并分析场景",
                    )
                    if rich_profile is None:
                        raise ValueError("缺少 rich_content 视觉模式配置")
                    prepare_call = (
                        prepare_session_visual_compare_item
                        if task.mode == "compare"
                        else prepare_session_rich_content_item
                    )
                    prepared = await asyncio.wait_for(
                        asyncio.to_thread(
                            prepare_call,
                            item_dict,
                            session_name=task.session_name,
                            item_index=idx,
                            total_items=len(task.items),
                            profile=rich_profile,
                        ),
                        timeout=float(runtime_options.get("video_prepare_timeout_s") or 300),
                    )
                    item_dict.clear()
                    item_dict.update(prepared)
                    _persist_task(task)
                    _frame_dir = ""
                    if item_dict.get("frames"):
                        _frame_dir = str(Path(item_dict["frames"][0]).parent)
                    elif item_dict.get("frames1"):
                        _frame_dir = str(Path(item_dict["frames1"][0]).parent)
                    log_event(
                        "视频准备",
                        "关键帧提取完成",
                        details={
                            "关键帧数": item_dict.get("frame_count"),
                            "抽帧目录": _frame_dir,
                        },
                        progress=12,
                        progress_message=f"关键帧提取完成（{item_dict.get('frame_count', 0)} 帧）",
                    )
                except Exception as e:
                    last_error = e
                    log_event(
                        "视频准备",
                        "失败",
                        level=logging.ERROR,
                        details=error_details(e),
                        progress=12,
                        progress_message="视频校验或抽帧失败",
                        progress_status="error",
                    )
            if last_error is None:
                if MODEL_LIMITER.would_block():
                    # 展示层提示：预处理完成但模型槽全忙，需排队（可能与实际
                    # 获取存在良序竞态，最坏多发/少发一条事件，不影响功能）
                    log_event(
                        "模型调度",
                        "等待模型槽位",
                        details={
                            "模型运行中": MODEL_LIMITER.stats()["running"],
                            "模型排队中": MODEL_LIMITER.stats()["queued"],
                        },
                        progress=13,
                        progress_message="等待模型调用槽位",
                    )
                # 模型调用级并发槽：只在模型阶段持有，视频预处理不占槽；
                # priority=True 插队头（组第 2+ 轮 / 批第 2+ 条优先续队）。
                # wait_for 只包 _eval_one——排队等待不消耗单题超时。
                async with MODEL_LIMITER.slot(priority=priority):
                    for attempt in range(2):
                        # 每次尝试现读全局设置：运行中调整超时对后续轮次/题目生效
                        eval_timeout = get_settings().eval_timeout_s
                        try:
                            if attempt:
                                log_event(
                                    "单题评测",
                                    "开始外层重试",
                                    level=logging.WARNING,
                                    details={"请求次数": f"{attempt + 1}/2"},
                                    progress=15,
                                    progress_message="正在重新执行单题评测",
                                )
                            res = await asyncio.wait_for(
                                _eval_one(
                                    task.mode, idx, item_dict,
                                    rich_judges=rich_judges,
                                    compare_judges=compare_judges,
                                    category_display=category_display,
                                ),
                                timeout=eval_timeout,
                            )
                            break
                        except asyncio.TimeoutError:
                            last_error = TimeoutError(f"单题评估超过 {eval_timeout:.0f} 秒")
                            log_event(
                                "单题评测",
                                "超时",
                                level=logging.ERROR,
                                details=error_details(last_error),
                            )
                            break
                        except Exception as e:
                            last_error = e
                            retryable = is_retriable_llm_error(e)
                            will_retry = attempt == 0 and retryable
                            log_event(
                                "单题评测",
                                "失败，准备重试" if will_retry else "最终失败",
                                level=logging.WARNING if will_retry else logging.ERROR,
                                details={
                                    "请求次数": f"{attempt + 1}/2",
                                    "可重试": retryable,
                                    **error_details(e),
                                },
                            )
                            if will_retry:
                                await asyncio.sleep(0.7)  # 与模型调用层的固定重试间隔一致
                                continue
                            break
            if res is None:
                res = {
                    "index": idx,
                    "item_id": item_id,
                    "query": item_dict.get("query", ""),
                    "error": f"{type(last_error).__name__}: {last_error}",
                }
                if item_dict.get("context"):
                    res["context"] = item_dict["context"]
                _write_eval_error(
                    task.id,
                    idx,
                    item_dict,
                    last_error,
                    request_id=request_id,
                )
            res["index"] = idx
            if pending_judge_traces:
                await asyncio.to_thread(
                    flush_web_trace_records,
                    pending_judge_traces,
                    res,
                )
            await finish(idx, res, started)
            return res

    async def fail(idx: int, item_dict: dict, reason: str) -> dict:
        """连坐失败：组内前序轮次失败后，为剩余轮次落一条 error 结果。

        结果形状与 one() 失败路径一致（index/item_id/query/error），走同一个
        finish 回调落结果、发 result 事件、持久化；进度侧发一条 error 事件，
        SSE 逐题进度即时可见。根因轮已写过 eval_errors.jsonl，这里不再重复。
        """
        request_id = make_request_id(task.created_at, task.id, idx)
        item_id = item_dict.get("id") or f"q{idx}"
        with bind_chain_context(
            task_id=task.id,
            session_name=task.session_name,
            request_id=request_id,
            item_id=item_id,
            item_index=idx,
            progress_callback=_progress_publisher(idx),
        ):
            log_event(
                "会话",
                "跳过评测",
                level=logging.WARNING,
                details={"原因": reason},
                progress=100,
                progress_message=reason,
                progress_status="error",
            )
            res = {
                "index": idx,
                "item_id": item_id,
                "query": item_dict.get("query", ""),
                "error": reason,
            }
            if item_dict.get("context"):
                res["context"] = item_dict["context"]
            await finish(idx, res, time.perf_counter())
            return res

    return one, fail, clients


async def _aclose_judge_clients(clients: list[JudgeClient]) -> None:
    """运行结束统一关闭裁判客户端（正常/异常/取消三条路径都经此）。"""
    results = await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("关闭裁判客户端失败: %s", r)


async def _run(task: Task, cfg: AppConfig) -> None:
    one, fail, clients = _make_item_evaluator(task, cfg)

    # 多轮垂域视觉评测：同一 session_group 的各轮按 turn_index 串行评测，
    # 评完一轮即生成 ≤120 字总结并注入下一轮 context。调度单位是「整组」或
    # 「独立题」，在 PIPELINE_LIMITER 上排队（流水线准入 = 并发 + 等待容量，
    # 跨任务共享）；模型槽在 one() 内逐题获取，组第 2+ 轮插队头优先续队
    # （预处理不占模型槽，组内轮次不被排到新到达者之后）。
    # session_group 由 parse_csv 据 is_start/is_end 切组赋值，与上游 session_id 列无关。
    sessions: dict[str, list[int]] = {}
    standalone: list[int] = []
    for i, it in enumerate(task.items):
        grp = it.get("session_group")
        if task.mode == "rich_content" and grp:
            sessions.setdefault(str(grp), []).append(i)
        else:
            standalone.append(i)
    for _grp, idxs in sessions.items():
        idxs.sort(key=lambda i: task.items[i].get("turn_index", 0))

    async def run_session(idxs: list[int]) -> None:
        """组内按轮次串行：把前序各轮总结累积写进当前轮 context 后再评测。
        总结直接取评测调用顺带产出的 turn_summary 字段，不再单独调用模型总结。
        任一轮失败即连坐：缺一轮信息的后续评测不可信，剩余轮次直接落
        「同组前序轮次失败」结果并提前终止（session 槽位随之释放）。"""
        prior_summary = ""
        for turn_no, idx in enumerate(idxs, 1):
            it = task.items[idx]
            if prior_summary:
                base_ctx = (it.get("context") or "").strip()
                it["context"] = (
                    f"{base_ctx}\n\n历史对话总结：\n{prior_summary}"
                    if base_ctx
                    else f"历史对话总结：\n{prior_summary}"
                )
            res = await one(idx, it, priority=turn_no > 1)  # 第 2+ 轮模型槽插队头
            if res.get("error") and turn_no < len(idxs):
                reason = f"同组前序轮次失败：{res['error']}"
                for later_idx in idxs[turn_no:]:
                    await fail(later_idx, task.items[later_idx], reason)
                return
            if turn_no == len(idxs):
                continue  # 最后一轮总结无人消费，跳过
            summary = (res.get("turn_summary") or "").strip()
            prior_summary += (
                f"【第{turn_no}轮】{summary}\n"
                if summary
                else f"【第{turn_no}轮】（未生成总结）\n"
            )

    async def _session_job(idxs: list[int]) -> None:
        # 整组占一个流水线准入槽（界定等待总量）；模型槽由组内各轮在
        # one() 中逐轮获取（第 2+ 轮插队头优先续队）
        async with PIPELINE_LIMITER:
            await run_session(idxs)

    async def _standalone_job(i: int) -> None:
        async with PIPELINE_LIMITER:
            await one(i, task.items[i])

    coros = [_session_job(idxs) for idxs in sessions.values()]
    coros += [_standalone_job(i) for i in standalone]
    try:
        await asyncio.gather(*coros)
    finally:
        await _aclose_judge_clients(clients)


# 登记后台更新批任务引用：避免协程被 GC，也便于测试等待完成。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_background(coro: Awaitable[None]) -> asyncio.Task:
    """后台启动一个评测协程并保留强引用，完成后自动移出登记表。"""
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


# ── 手动重跑 ────────────────────────────────────────────────────────────────
# 调度单位与 _run 相同：整组（rich_content 且带 session_group）或独立题；
# 选择驱动：每组从首个选中轮切到组尾，复用前序轮的 turn_summary 重建总结链。

# 视频预处理 / UI 提交写入 item 的运行时键；重跑副本剔除后强制重抽帧
# （与 merge_items_by_id 的整字典替换语义一致）
_RUNTIME_ITEM_KEYS = {
    "frames", "frames1", "frames2", "frame_count", "media", "video_name",
    "duration", "duration1", "duration2", "video1_path", "video2_path",
}
# run_session / 更新批注入历史总结所用的标记；重跑副本剥离上次注入的块，防止跨次累积
_SUMMARY_MARKER = "\n\n历史对话总结：\n"
_SUMMARY_MARKER_FLAT = "历史对话总结：\n"


def _strip_injected_summary(context: str) -> str:
    """剥掉上一次运行注入的「历史对话总结」块，还原原始 context。

    注入格式见 run_session / _run_update_batch_body：base 非空时为
    "{base}\\n\\n历史对话总结：\\n{链}"，base 为空时直接以标记开头。取首次
    出现位置截断即可幂等还原（多次注入只会叠在首次标记之后）。边界：用户
    context 恰含该字面量会被误截，属可接受的病态场景。
    """
    pos = context.find(_SUMMARY_MARKER)
    if pos != -1:
        return context[:pos].rstrip()
    if context.startswith(_SUMMARY_MARKER_FLAT):
        return ""
    return context


def _latest_result_by_index(task: Task) -> dict[int, dict]:
    """index → 最新一条结果（正向一趟后写覆盖，与 upsert_result_by_index 一致）。"""
    latest: dict[int, dict] = {}
    for r in task.results:
        idx = r.get("index")
        if isinstance(idx, int):
            latest[idx] = r
    return latest


def _rerun_item_copy(item: dict) -> dict:
    """重跑用 item 副本：剔除运行时键（强制重抽帧），剥离上次注入的历史总结块。

    必须传副本而非 task.items[i] 本体——one() 的视频预处理 clear()+update()
    与批体的 context 注入都是原地变更，直接传会污染 task.items 并随快照落盘。
    """
    copy = {k: v for k, v in item.items() if k not in _RUNTIME_ITEM_KEYS}
    context = _strip_injected_summary(copy.get("context") or "")
    if context:
        copy["context"] = context
    else:
        copy.pop("context", None)
    return copy


@dataclass
class RerunBatch:
    """一个重跑批：batch 为 (index, 干净 item 副本) 列表，顺序即轮次顺序。"""

    batch: list[tuple[int, dict]]
    initial_summary: str  # 复用前序轮总结重建的种子总结链
    initial_turn: int     # 种子链覆盖的前缀轮数；批内轮次编号从 initial_turn+1 接续
    group: str            # 组名 / standalone:{index}


def build_rerun_batches(task: Task, indexes: list[int]) -> list[RerunBatch]:
    """把选中的 item index 展开为重跑批（组展开/切片规则的服务端权威实现）。

    选择驱动、不限失败项：成功/失败/未评估条目均可重跑。组展开与 _run 一致：
    rich_content 且带 session_group 的进组，其余为独立题。每个被选中的组从
    首个「选中」轮切到组尾（后轮依赖前轮重评后的新总结，必须连着重跑）；
    切片前缀的结果不动，其 turn_summary 重建为种子总结链在批内接着注入。
    独立题选中即单题批。返回 batches（每个选中组/独立题恰一批）。
    """
    latest = _latest_result_by_index(task)

    selected = {i for i in indexes if isinstance(i, int)}

    # 与 _run 相同的分组规则：组名 → 组内全部 index（按 turn_index 排序）
    sessions: dict[str, list[int]] = {}
    standalone_selected: list[int] = []
    for i, it in enumerate(task.items):
        grp = it.get("session_group")
        if task.mode == "rich_content" and grp:
            sessions.setdefault(str(grp), []).append(i)
        elif i in selected:
            standalone_selected.append(i)
    for idxs in sessions.values():
        idxs.sort(key=lambda i: task.items[i].get("turn_index", 0))

    batches: list[RerunBatch] = []
    for grp, idxs in sessions.items():
        first_sel = next((p for p, i in enumerate(idxs) if i in selected), None)
        if first_sel is None:
            continue
        initial_summary = ""
        for pos, i in enumerate(idxs[:first_sel], 1):
            summary = (latest[i].get("turn_summary") or "").strip() if i in latest else ""
            initial_summary += (
                f"【第{pos}轮】{summary}\n"
                if summary
                else f"【第{pos}轮】（未生成总结）\n"
            )
        batches.append(RerunBatch(
            batch=[(i, _rerun_item_copy(task.items[i])) for i in idxs[first_sel:]],
            initial_summary=initial_summary,
            initial_turn=first_sel,
            group=grp,
        ))
    for i in standalone_selected:
        batches.append(RerunBatch(
            batch=[(i, _rerun_item_copy(task.items[i]))],
            initial_summary="",
            initial_turn=0,
            group=f"standalone:{i}",
        ))
    return batches


def _all_items_healthy(task: Task) -> bool:
    """每个 item index 的最新结果都存在且无 error（latest-wins 语义）。"""
    latest = _latest_result_by_index(task)
    return all(i in latest and not latest[i].get("error") for i in range(len(task.items)))


async def _run_update_batch_body(
    task: Task,
    cfg: AppConfig,
    batch: list[tuple[int, dict]],
    *,
    options: dict,
    manage_status: bool = False,
    initial_summary: str = "",
    initial_turn: int = 0,
) -> None:
    """后台更新批：batch 内全部条目按提交顺序作为一个串行会话评测；
    每题结果按 index 原地覆盖/追加（后完成者赢），全量重算 summary 并落快照。

    不做任务级状态迁移、不动 done_total、不发 start 事件；manage_status=True
    仅供"本接口新建的任务"使用（否则任务永远停在 pending，重启后会被
    get_task 误判为服务中断，且已连接的 SSE 流收不到终态）。

    initial_summary/initial_turn 供失败重跑使用：种子总结链（前序好轮复用）
    及其覆盖的前缀轮数，批内轮次编号从 initial_turn+1 接续编，避免重跑批
    从 1 重开导致总结链出现重复的【第1轮】标签。
    """
    batch_done = 0  # 批内已完成数（批次口径计数，区别于全量 len(task.results)）

    async def _merge_on_result(idx: int, res: dict, started: float) -> None:
        nonlocal batch_done
        batch_done += 1
        action = upsert_result_by_index(task, res)
        # summary 全量重算移入 _flush_now（随节流后的落盘一起做），
        # 不再每题重算 O(n)
        failed = bool(res.get("error"))
        log_event(
            "任务",
            "完成",
            level=logging.ERROR if failed else logging.INFO,
            details={
                "状态": "失败" if failed else "成功",
                "合并": action,
                "总耗时": f"{time.perf_counter() - started:.2f}秒",
                "错误": res.get("error"),
            },
            progress=100,
            progress_message="评测失败" if failed else "评测完成",
            progress_status="error" if failed else "done",
        )
        await task.publish(
            "result",
            {
                "progress": batch_done,
                "total": len(batch),
                "batch": True,
                "result": res,
            },
        )
        _persist_task(task)

    one, fail, clients = _make_item_evaluator(
        task, cfg, options=options, on_result=_merge_on_result
    )
    if manage_status:
        task.status = "running"
        _persist_task(task, force=True)
    current: tuple[int, dict] | None = None
    try:
        # 整批一个串行会话：占一个流水线准入槽跑完整批（界定等待总量）；
        # 模型槽由批内各条在 one() 中逐条获取（第 2+ 条插队头优先续队）。
        # 前轮总结在批次内本地链式注入，不从 task.results 读回，
        # 不受并行批次覆盖影响。任一轮失败即连坐：剩余条目直接落
        # 「同组前序轮次失败」结果并提前终止（槽位随之释放）。
        async with PIPELINE_LIMITER:
            prior_summary = initial_summary
            for pos, (idx, item_dict) in enumerate(batch, 1):
                turn_no = pos + initial_turn  # 总结链编号延续原组轮次
                current = (idx, item_dict)
                if prior_summary:
                    base_ctx = (item_dict.get("context") or "").strip()
                    item_dict["context"] = (
                        f"{base_ctx}\n\n历史对话总结：\n{prior_summary}"
                        if base_ctx
                        else f"历史对话总结：\n{prior_summary}"
                    )
                task.in_flight_indexes.add(idx)
                try:
                    res = await one(idx, item_dict, priority=pos > 1)
                finally:
                    task.in_flight_indexes.discard(idx)
                if res.get("error") and pos < len(batch):
                    reason = f"同组前序轮次失败：{res['error']}"
                    for later_idx, later_item in batch[pos:]:
                        await fail(later_idx, later_item, reason)
                    break
                if pos == len(batch):
                    continue  # 最后一轮总结无人消费，跳过
                summary = (res.get("turn_summary") or "").strip()
                prior_summary += (
                    f"【第{turn_no}轮】{summary}\n"
                    if summary
                    else f"【第{turn_no}轮】（未生成总结）\n"
                )
        if manage_status:
            task.status = "done"
            task.summary = _summarize(task)  # publish 前重算（节流后不再每题重算）
            await task.publish(
                "done", {"summary": task.summary, "total": len(task.items)}
            )
            _persist_task(task, force=True)
    except Exception as e:
        # one() 内部已把单题异常转成 error res；这里只兜底批级异常
        # （如持久化 I/O 崩溃），避免静默吞掉。
        logger.exception("更新批评测失败: task_id=%s", task.id)
        if current:
            _write_eval_error(task.id, current[0], current[1], e)
        if manage_status:
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            await task.publish("error", {"message": task.error})
        _persist_task(task, force=True)
    finally:
        await _aclose_judge_clients(clients)


async def run_update_batch(
    task: Task,
    cfg: AppConfig,
    batch: list[tuple[int, dict]],
    *,
    options: dict,
    manage_status: bool = False,
    initial_summary: str = "",
    initial_turn: int = 0,
) -> None:
    """后台更新批公共入口（实现见 _run_update_batch_body）。

    pin 契约（R1）同 run_eval：调用方在提交时同步 `active_runs += 1`。
    外层负责退休前最后一次落盘、从 TASKS 注册表退休；并行批次共享同一活
    对象（计数 pin），全部结束（idle）时才退休。
    """
    try:
        await _run_update_batch_body(
            task, cfg, batch, options=options, manage_status=manage_status,
            initial_summary=initial_summary, initial_turn=initial_turn,
        )
    finally:
        task.active_runs -= 1
        idle = task.active_runs <= 0
        interrupted = _mark_interrupted_if_stuck(task) if idle else False
        if idle and not manage_status and task.status == "error" and _all_items_healthy(task):
            # 重跑修复全部坏项：error 任务 heal 为 done，让下方 R4 补发
            # done+新 summary；否则中断过的任务重跑成功后仍挂着 error 终态。
            # manage_status 批的终态由 body 自己发过，不在 heal 范围内。
            task.status = "done"
            task.error = None
        _persist_task(task, force=True)  # 退休前最后一次落盘，磁盘先于内存下线
        if idle and not manage_status and not interrupted and task.status in {"done", "error"}:
            # R4：manage_status=False 的批不发 start/done 终态事件，SSE 订阅者
            # 会一直等；最后一个批结束时补发一次终态（先 persist 再发，summary
            # 已在 _flush_now 重算）。manage_status=True 的终态由 body 发过。
            task._fanout(
                "done" if task.status == "done" else "error",
                (
                    {"summary": task.summary, "total": len(task.items)}
                    if task.status == "done"
                    else {"message": task.error}
                ),
            )
        retire_task(task)


def _write_eval_error(
    task_id: str,
    idx: int,
    item: dict,
    error: Exception | None,
    *,
    request_id: str = "",
) -> None:
    """持久化最终失败，避免内存任务结束后无法定位批跑异常。"""
    try:
        path = RUNS_DIR / "eval_errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "task_id": task_id,
            "request_id": request_id,
            "index": idx,
            "item_id": item.get("id") or f"q{idx}",
            "query": item.get("query", ""),
            "context": item.get("context", ""),
            "error": f"{type(error).__name__}: {error}" if error else "unknown",
            "traceback": "".join(traceback.format_exception(error)) if error else "",
        }
        raw_output = getattr(error, "raw_output", None)
        repair_output = getattr(error, "repair_output", None)
        if raw_output is not None or repair_output is not None:
            record.update({
                "stage": "judge_json_parse",
                "judge": getattr(error, "judge", None),
                "model": getattr(error, "model", None),
                "original_model_output": raw_output,
                "repair_model_output": repair_output,
                "original_output_length": len(raw_output or ""),
                "repair_output_length": len(repair_output or ""),
            })
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _eval_one(
    mode,
    idx,
    item_dict,
    *,
    rich_judges=None,
    compare_judges=None,
    category_display=None,
) -> dict:
    t0 = time.perf_counter()
    item = _to_evalitem(item_dict, idx)
    out: dict = {"query": item.question}
    if item.context:
        out["context"] = item.context

    if mode == "rich_content":
        if not rich_judges:
            raise ValueError("没有可用的垂域视觉评测裁判")
        frames = [str(path) for path in (item.metadata.get("frames") or [])]
        if not frames:
            raise ValueError("垂域视觉评测缺少关键帧")
        answer_text = str(item_dict.get("answer_text") or "").strip()
        if answer_text:
            out["answer_text"] = answer_text
        # 事实参考答案（仅用于 factual_conflict 判定，不代表正确答案；
        # 空值时裁判侧省略该维度，结果归一为 no）
        reference_answer = str(item_dict.get("reference_answer") or "").strip()
        # 视觉事实列表不做多裁判模糊合并；使用用户选择顺序中的第一位裁判，
        # 保证 presence/count/items 始终来自同一份自洽观察。
        visual = await rich_judges[0].evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            answer_text=answer_text,
            reference_answer=reference_answer,
            frames=frames,
        )
        out.update(visual)
        log_event(
            "结果聚合",
            "视觉发现已结构化",
            details={
                "挂卡数": out.get("card_count"),
                "Superlink数": out.get("superlink_count"),
                "需复核": out.get("needs_review"),
                "是否解决问题": out.get("problem_solved"),
            },
            progress=90,
            progress_message="正在整理垂域视觉评测结果",
        )

    else:  # compare
        if not compare_judges:
            raise ValueError("没有可用的垂域视觉对比评测裁判")
        frames1 = item_dict.get("frames1") or []
        frames2 = item_dict.get("frames2") or []
        if not frames1 or not frames2:
            raise ValueError("垂域视觉对比评测缺少关键帧")

        answer1 = str(item_dict.get("answer1") or "").strip()
        answer2 = str(item_dict.get("answer2") or "").strip()
        context1 = str(item_dict.get("context1") or "").strip()
        context2 = str(item_dict.get("context2") or "").strip()

        out["answer1"] = answer1
        out["answer2"] = answer2
        out["context1"] = context1
        out["context2"] = context2

        compare_result = await compare_judges[0].evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            context1=context1,
            answer1=answer1,
            frames1=[str(p) for p in frames1],
            context2=context2,
            answer2=answer2,
            frames2=[str(p) for p in frames2],
        )
        out.update(compare_result)
        log_event(
            "结果聚合",
            "视觉对比完成",
            details={
                "相关性": compare_result.get("relevance"),
                "安全合规": compare_result.get("safety"),
                "内容质量": compare_result.get("content_quality"),
                "需求闭环": compare_result.get("need_closure"),
                "个性化": compare_result.get("personalization"),
                "内容冲突": compare_result.get("has_conflict"),
            },
            progress=90,
            progress_message="垂域视觉对比评测完成",
        )

    # 实际归属垂域 + 题号，供按垂域聚合；未配置映射时显示原始 category
    out["item_id"] = item.id
    out["category"] = item.category
    out["category_display"] = (category_display or {}).get(item.category) or (
        item.category if item.category != "default" else "通用"
    )
    out["latency_s"] = round(time.perf_counter() - t0, 1)  # 该题评测总耗时（秒）
    return out


def _summarize(task: Task) -> dict:
    if task.mode == "rich_content":
        return _summarize_rich_content(task)
    # compare：五维胜负 + 内容冲突统计
    res = task.results
    ok = [r for r in res if "error" not in r]
    summary: dict = {
        "total": len(res),
        "done": len(ok),
        "failed": len(res) - len(ok),
        "mode": task.mode,
    }
    for dim in ["relevance", "safety", "content_quality", "need_closure", "personalization"]:
        a_wins = sum(1 for r in ok if r.get(dim) == "answer1")
        b_wins = sum(1 for r in ok if r.get(dim) == "answer2")
        ties = sum(1 for r in ok if r.get(dim) == "tie")
        na = sum(1 for r in ok if r.get(dim) is None)
        total = a_wins + b_wins + ties
        summary[f"{dim}_answer1_wins"] = a_wins
        summary[f"{dim}_answer2_wins"] = b_wins
        summary[f"{dim}_ties"] = ties
        summary[f"{dim}_na"] = na
        summary[f"{dim}_answer1_rate"] = round(a_wins / total, 3) if total else None
    summary["conflict_yes"] = sum(1 for r in ok if r.get("has_conflict") == "yes")
    summary["conflict_no"] = sum(1 for r in ok if r.get("has_conflict") == "no")
    summary["conflict_unclear"] = sum(1 for r in ok if r.get("has_conflict") == "unclear")
    return summary


def _summarize_rich_content(task: Task) -> dict:
    """汇总视觉发现与整体评价，不使用问答类 correctness/准确率口径。"""
    results = task.results
    ok = [row for row in results if "error" not in row]
    card_cases = [row for row in ok if row.get("card_presence") == "present"]
    superlink_cases = [
        row for row in ok if row.get("superlink_presence") == "present"
    ]
    complete = [row for row in ok if row.get("answer_coverage") == "complete"]
    solved_ok = [row for row in ok if row.get("problem_solved") == "ok"]
    solved_nok = [row for row in ok if row.get("problem_solved") == "nok"]
    solved_review = [row for row in ok if row.get("problem_solved") == "need_review"]
    # 事实冲突（仅与 reference_answer 比对；无参考答案的行归一为 no，不计入）
    factual_conflict_yes = [
        row for row in ok if row.get("factual_conflict") == "yes"
    ]
    both = [
        row for row in ok
        if row.get("card_presence") == "present"
        and row.get("superlink_presence") == "present"
    ]
    neither = [
        row for row in complete
        if row.get("card_presence") == "absent"
        and row.get("superlink_presence") == "absent"
    ]

    by_category: dict[str, dict] = {}
    for row in ok:
        category = str(row.get("category") or "default")
        entry = by_category.setdefault(category, {
            "category": category,
            "display": row.get("category_display") or category,
            "count": 0,
            "card_cases": 0,
            "superlink_cases": 0,
            "solved_ok": 0,
            "solved_nok": 0,
            "solved_review": 0,
        })
        entry["count"] += 1
        entry["card_cases"] += int(row.get("card_presence") == "present")
        entry["superlink_cases"] += int(
            row.get("superlink_presence") == "present"
        )
        entry["solved_ok"] += int(row.get("problem_solved") == "ok")
        entry["solved_nok"] += int(row.get("problem_solved") == "nok")
        entry["solved_review"] += int(row.get("problem_solved") == "need_review")

    return {
        "total": len(results),
        "done": len(ok),
        "failed": len(results) - len(ok),
        "mode": task.mode,
        "card_case_count": len(card_cases),
        "card_presence_rate": (
            round(len(card_cases) / len(ok), 3) if ok else None
        ),
        "card_total": sum(int(row.get("card_count") or 0) for row in ok),
        "superlink_case_count": len(superlink_cases),
        "superlink_presence_rate": (
            round(len(superlink_cases) / len(ok), 3) if ok else None
        ),
        "superlink_total_observed": sum(
            int(row.get("superlink_count") or 0) for row in ok
        ),
        "both_count": len(both),
        "neither_count": len(neither),
        "needs_review_count": sum(bool(row.get("needs_review")) for row in ok),
        "complete_coverage_count": len(complete),
        "solved_ok": len(solved_ok),
        "solved_nok": len(solved_nok),
        "solved_review": len(solved_review),
        "factual_conflict_yes": len(factual_conflict_yes),
        "solved_ok_rate": (
            round(len(solved_ok) / len(ok), 3) if ok else None
        ),
        "by_category": sorted(
            by_category.values(),
            key=lambda entry: (-entry["count"], entry["category"]),
        ),
    }
