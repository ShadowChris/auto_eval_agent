"""评估执行：分发三种模式 + 并发 + 推 SSE 事件 + 元评测汇总。

复用 auto_eval 核心：RubricJudge / PairwiseJudge / aggregate_* / build_runner / ground_truth。
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from ..analysis.operation_statistics import summarize_operation_results
from ..paths import RUNS_DIR
from ..config import AppConfig
from ..dataset import to_prompt
from ..judges import (
    Arbitrator,
    JudgeClient,
    PairwiseJudge,
    RichContentJudge,
    RubricJudge,
    SkillRouter,
    _format_visual_findings_for_rubric,
    ensure_classified,
)
from ..judges.base import flush_web_trace_records
from ..judges.ensemble import (
    aggregate_operation_scores,
    aggregate_pairs,
    aggregate_scores,
)
from ..llm_stream import is_retriable_llm_error
from ..meta import ground_truth
from ..observability import (
    bind_chain_context,
    error_details,
    log_event,
    make_request_id,
)
from ..runners import build_runner
from ..schema import EvalItem
from .history import save_task
from .operation_media import (
    prepare_session_operation_item,
    prepare_session_rich_content_item,
)
from .tasks import Task, prune_task_cache


logger = logging.getLogger(__name__)
MAX_PROGRESS_EVENTS_PER_ITEM = 100
SNAPSHOT_PERSIST_INTERVAL_S = 1.0


def _default_task_concurrency(task: Task) -> int:
    return 8 if task.mode == "operation" else 4


def _selected_judge_configs(task: Task, cfg: AppConfig) -> list:
    """解析评估视角；任务类固定终端用户，模型服务与其独立。"""
    if task.mode == "operation":
        terminal = next(
            (judge for judge in cfg.judges if judge.persona == "end_user"),
            None,
        )
        if terminal is None:
            terminal = next(
                (
                    judge
                    for judge in cfg.judges
                    if str(judge.display or "").strip() == "终端用户"
                ),
                None,
            )
        if terminal is None:
            raise ValueError("任务类评估缺少终端用户裁判配置")
        return [terminal]
    selected = list(task.options.get("judges") or [])
    matched = [judge for judge in cfg.judges if judge.name in selected]
    if matched:
        return matched
    return cfg.judges[:1]


def _persist_task(task: Task, *, force: bool = False) -> bool:
    """Persist without allowing history I/O to break the evaluation/SSE."""
    now = time.monotonic()
    if (
        not force
        and task.last_persist_at > 0
        and now - task.last_persist_at < SNAPSHOT_PERSIST_INTERVAL_S
    ):
        return True
    try:
        saved = bool(save_task(task))
        if saved:
            task.last_persist_at = now
        return saved
    except Exception:
        logger.exception("unexpected task snapshot failure: task_id=%s", task.id)
        return False


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
    task.publish_nowait("item_progress", event_payload)
    return event_payload


def _to_evalitem(item: dict, idx: int) -> EvalItem:
    meta = dict(item.get("metadata") or {})
    if item.get("frames"):
        meta["frames"] = item["frames"]  # operation：抽好的关键帧路径，裁判读取后 encode 成 image_url
    return EvalItem(
        id=item.get("id", f"q{idx}"),
        question=item["query"],
        context=item.get("context"),
        has_ref=bool(item.get("reference")),
        reference=item.get("reference"),
        category=item.get("category", "default"),
        trace=item.get("trace"),
        media=item.get("media") or [],
        metadata=meta,
    )


async def run_eval(task: Task, cfg: AppConfig) -> None:
    task.mark_started()
    await task.publish("start", {"total": len(task.items), "mode": task.mode})
    task.status = "running"
    _persist_task(task, force=True)
    try:
        await _run(task, cfg)
        task.summary = _summarize(task, cfg)
        task.status = "done"
        task.mark_finished()
        _persist_task(task, force=True)
        await task.publish(
            "done",
            {
                "summary": task.summary,
                "total": len(task.items),
                "duration_s": task.duration_s,
            },
        )
    except Exception as e:
        task.status = "error"
        task.error = f"{type(e).__name__}: {e}"
        task.mark_finished()
        _persist_task(task, force=True)
        await task.publish(
            "error",
            {"message": task.error, "duration_s": task.duration_s},
        )
    finally:
        prune_task_cache(keep_task_ids={task.id})


def _result_index(result: dict) -> int | None:
    try:
        return int(result.get("index"))
    except (TypeError, ValueError):
        return None


def _upsert_result(task: Task, result: dict) -> dict | None:
    """按原始数据集索引替换最新结果，保持输入顺序且不产生重复行。"""
    index = _result_index(result)
    previous = None
    if index is not None:
        for position, existing in enumerate(task.results):
            if _result_index(existing) == index:
                previous = existing
                task.results[position] = result
                break
        else:
            task.results.append(result)
    else:
        task.results.append(result)
    task.results.sort(
        key=lambda row: (
            _result_index(row) is None,
            _result_index(row) if _result_index(row) is not None else len(task.items),
        ),
    )
    task.done_total = len({
        value for row in task.results
        if (value := _result_index(row)) is not None
    })
    return previous


def _valid_cached_frames(item: dict) -> bool:
    frames = item.get("frames") or []
    return bool(frames) and all(Path(str(frame)).is_file() for frame in frames)


def _is_multi_group_operation(item: dict) -> bool:
    return bool(item.get("group_variants"))


def _prepare_rerun_items(task: Task, item_indices: list[int]) -> None:
    """复用有效抽帧；缓存缺失时回退到原视频重新抽取。"""
    if task.mode not in {"operation", "rich_content", "rich_content_quality"}:
        return
    for index in item_indices:
        item = task.items[index]
        if task.mode == "operation" and _is_multi_group_operation(item):
            for variant in item.get("group_variants") or []:
                group_item = variant.get("item")
                if not isinstance(group_item, dict):
                    continue
                group_item.pop("prepare_error", None)
                if group_item.get("frames") and not _valid_cached_frames(group_item):
                    group_item.pop("frames", None)
                    group_item.pop("frame_count", None)
            continue
        if item.get("frames") and not _valid_cached_frames(item):
            item.pop("frames", None)
            item.pop("frame_count", None)


async def run_rerun(
    task: Task,
    cfg: AppConfig,
    item_indices: list[int],
    *,
    base_status: str | None = None,
) -> None:
    """在原历史任务内重跑指定原始索引，并把最新结果原位合并。"""
    pending_attempt = (
        dict(task.active_rerun)
        if task.active_rerun and task.active_rerun.get("status") == "starting"
        else {}
    )
    attempt_no = int(pending_attempt.get("attempt_no") or len(task.rerun_history) + 1)
    started_at = float(pending_attempt.get("started_at") or time.time())
    base_status = base_status or (
        task.status if task.status in {"done", "error", "cancelled"} else "done"
    )
    base_error = task.error
    attempt = {
        "attempt_id": pending_attempt.get("attempt_id") or f"rerun-{attempt_no}-{uuid.uuid4().hex[:8]}",
        "attempt_no": attempt_no,
        "item_indices": list(item_indices),
        "total": len(item_indices),
        "done": 0,
        "status": "running",
        "base_status": base_status,
        "started_at": started_at,
        "judge_backend": dict(pending_attempt.get("judge_backend") or {}),
        "items": [],
    }
    task.active_rerun = attempt
    task.status = "rerunning"
    task.error = None
    _prepare_rerun_items(task, item_indices)
    for index in item_indices:
        item = task.items[index]
        _record_progress(task, index, {
            "item_index": index,
            "item_id": item.get("id") or f"q{index}",
            "status": "pending",
            "percent": 0,
            "message": "等待重跑",
            "stage_rank": 0,
            "started_at": None,
            "finished_at": None,
            "attempt_id": attempt["attempt_id"],
            "attempt_no": attempt_no,
        })
    _persist_task(task, force=True)
    await task.publish("rerun_start", {**attempt, "items": []})
    cancelled = False
    try:
        await _run(task, cfg, item_indices=item_indices, rerun=attempt)
        attempt["status"] = "done"
    except asyncio.CancelledError:
        cancelled = True
        attempt["status"] = "cancelled"
        attempt["error"] = "用户手动中断重跑"
    except Exception as exc:
        attempt["status"] = "error"
        attempt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        finished_at = time.time()
        attempt["finished_at"] = finished_at
        attempt["duration_s"] = round(max(0.0, finished_at - started_at), 3)
        task.rerun_history.append(dict(attempt))
        task.active_rerun = None
        task.summary = _summarize(task, cfg)
        unique_count = len({
            value for row in task.results
            if (value := _result_index(row)) is not None
        })
        task.status = "done" if unique_count >= len(task.items) else base_status
        if task.status not in {"done", "error", "cancelled"}:
            task.status = "done"
        task.error = None if task.status == "done" else base_error
        _persist_task(task, force=True)
        await task.publish(
            "rerun_cancelled" if cancelled else "rerun_done",
            {
                "attempt": attempt,
                "summary": task.summary,
                "status": task.status,
                "progress": task.done_total,
                "total": len(task.items),
            },
        )
        prune_task_cache(keep_task_ids={task.id})


async def run_single_api_item(
    task: Task,
    cfg: AppConfig,
    item_index: int,
    item_id: str,
    *,
    rerun: dict | None = None,
) -> None:
    """执行接口提交的一题；不同 item_id 可并行，结果仍原位合并。"""
    if task.single_api_semaphore is None:
        concurrency = max(
            1,
            int(task.options.get("concurrency", _default_task_concurrency(task))),
        )
        task.single_api_semaphore = asyncio.Semaphore(concurrency)
    semaphore = task.single_api_semaphore
    task.mark_started()
    task.status = "running"
    _prepare_rerun_items(task, [item_index])
    _record_progress(task, item_index, {
        "item_index": item_index,
        "item_id": item_id,
        "status": "pending",
        "percent": 0,
        "message": "等待评估",
        "stage_rank": 0,
        "started_at": None,
        "finished_at": None,
        "attempt_id": (rerun or {}).get("attempt_id"),
        "attempt_no": int((rerun or {}).get("attempt_no") or 0),
    })
    _persist_task(task, force=True)
    await task.publish("start", {"total": len(task.items), "mode": task.mode})

    try:
        async with semaphore:
            await _run(
                task,
                cfg,
                item_indices=[item_index],
                rerun=rerun,
            )
        if rerun is not None:
            rerun["status"] = "done"
    except asyncio.CancelledError:
        if rerun is not None:
            rerun["status"] = "cancelled"
            rerun["error"] = "用户手动中断重跑"
        raise
    except Exception as exc:
        logger.exception(
            "single API item evaluation failed: task_id=%s item_id=%s",
            task.id,
            item_id,
        )
        if rerun is not None:
            rerun["status"] = "error"
            rerun["error"] = f"{type(exc).__name__}: {exc}"
        task.error = f"{type(exc).__name__}: {exc}"
    finally:
        finished_at = time.time()
        if rerun is not None:
            rerun["finished_at"] = finished_at
            started_at = float(rerun.get("started_at") or finished_at)
            rerun["duration_s"] = round(max(0.0, finished_at - started_at), 3)
            audit_attempt = dict(rerun)
            audit_attempt.pop("_previous_result", None)
            task.rerun_history.append(audit_attempt)
            task.rerun_history.sort(
                key=lambda attempt: int(attempt.get("attempt_no") or 0),
            )
            task.single_api_attempts.pop(item_id, None)

        task.item_executions.pop(item_id, None)
        task.summary = _summarize(task, cfg)
        if task.item_executions:
            if task.status != "cancelled":
                task.status = "running"
        elif task.status != "cancelled":
            task.status = "error" if task.error else "done"
            task.mark_finished()
        _persist_task(task, force=True)

        if not task.item_executions and task.status == "done":
            await task.publish(
                "done",
                {
                    "summary": task.summary,
                    "total": len(task.items),
                    "duration_s": task.duration_s,
                },
            )
        elif not task.item_executions and task.status == "error":
            await task.publish(
                "error",
                {"message": task.error, "duration_s": task.duration_s},
            )
        prune_task_cache(keep_task_ids={task.id})


async def _run(
    task: Task,
    cfg: AppConfig,
    *,
    item_indices: list[int] | None = None,
    rerun: dict | None = None,
) -> None:
    judges_cfg = _selected_judge_configs(task, cfg)
    run_backend = dict(
        ((rerun or {}).get("judge_backend") or {})
        if rerun is not None
        else (task.options.get("judge_backend") or {})
    )
    run_models = list(dict.fromkeys(
        str(judge.model or judge.name) for judge in judges_cfg
    ))
    run_provider_name = str(
        run_backend.get("provider_name") or "角色默认配置"
    )
    run_provider_id = str(run_backend.get("provider_id") or "")
    run_model = str(run_backend.get("model") or "；".join(run_models))
    run_provider_revision = str(
        run_backend.get("provider_revision") or ""
    )
    evaluation_time = datetime.fromtimestamp(task.created_at).astimezone()
    _providers = cfg.eval_options.effective_providers()
    clients = [
        JudgeClient(j, _providers, cfg.eval_options.search_topk)
        for j in judges_cfg
    ]
    skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
    operation_knowledge = cfg.expert_knowledge.get("operation")
    rubrics = [
        RubricJudge(
            c,
            cfg.rubrics,
            skill_router,
            evaluation_time=evaluation_time,
            expert_knowledge=operation_knowledge,
        )
        for c in clients
    ]
    pair_judges = [PairwiseJudge(c, evaluation_time=evaluation_time) for c in clients]
    rich_profile = cfg.visual_modes.get("rich_content")
    rich_judges = (
        [RichContentJudge(client, rich_profile) for client in clients]
        if rich_profile is not None
        else []
    )
    # rich_content_quality：挂卡识别裁判可独立于回答评测裁判
    visual_judge = None
    if task.mode == "rich_content_quality" and rich_profile is not None:
        visual_judge_name = task.options.get("visual_judge")
        visual_judge_cfg = next(
            (j for j in cfg.judges if j.name == visual_judge_name),
            judges_cfg[0] if judges_cfg else None,
        )
        if visual_judge_cfg is not None:
            visual_client = JudgeClient(
                visual_judge_cfg, _providers, cfg.eval_options.search_topk,
            )
            visual_judge = RichContentJudge(visual_client, rich_profile, prompt_variant="rich_content_quality")
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    sem = asyncio.Semaphore(int(
        task.options.get("concurrency", _default_task_concurrency(task))
    ))
    eval_timeout = float(task.options.get("eval_timeout_s") or task.options.get("eval_timeout") or 300.0)

    online_runner = None
    if task.mode == "online":
        model_name = task.options.get("model") or cfg.models[0].name
        mc = next((m for m in cfg.models if m.name == model_name), cfg.models[0])
        online_runner = build_runner(mc)
    process_dims = cfg.process_rubrics
    arbitrator = (
        Arbitrator(
            clients[0],
            evaluation_time=evaluation_time,
            expert_knowledge=operation_knowledge,
        )
        if len(judges_cfg) >= 2
        else None
    )
    loop = asyncio.get_running_loop()

    async def one(idx: int, item_dict: dict):
        attempt_no = int((rerun or {}).get("attempt_no") or 0)
        attempt_id = str((rerun or {}).get("attempt_id") or "")
        request_id = make_request_id(
            task.created_at,
            task.id,
            idx,
            attempt_no=attempt_no,
        )
        pending_judge_traces: list[tuple[str, dict]] = []

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

        item_id = item_dict.get("id") or f"q{idx}"

        def collect_judge_trace(trace_path: str, record: dict) -> None:
            pending_judge_traces.append((trace_path, record))

        with bind_chain_context(
            task_id=task.id,
            session_name=task.session_name,
            request_id=request_id,
            item_id=item_id,
            item_index=idx,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
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
            async with sem:
                # 排队时间不计入单题耗时；取得并发槽后才启动计时。
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
                if task.mode == "operation" and _is_multi_group_operation(item_dict):
                    variants = item_dict.get("group_variants") or []
                    total_group_items = max(1, len(task.items) * max(1, len(variants)))
                    image_counts: list[dict] = []
                    for group_position, variant in enumerate(variants):
                        group_item = variant.get("item")
                        if not isinstance(group_item, dict):
                            image_counts.append({
                                "group_id": variant.get("group_id"),
                                "group_name": variant.get("group_name"),
                                "status": "missing_input",
                                "count": 0,
                            })
                            continue
                        if _valid_cached_frames(group_item):
                            group_item.pop("prepare_error", None)
                            image_counts.append({
                                "group_id": variant.get("group_id"),
                                "group_name": variant.get("group_name"),
                                "status": "ready",
                                "count": len(group_item.get("frames") or []),
                            })
                            continue
                        try:
                            log_event(
                                "视频准备",
                                f"校验并抽帧：{variant.get('group_name') or variant.get('group_id')}",
                                details={"视频路径": group_item.get("video_path")},
                                progress=3,
                                progress_message="正在校验多组视频并分析场景",
                            )
                            prepared_input = dict(group_item)
                            prepared_input["id"] = (
                                f"{item_id}__{variant.get('group_id') or group_position + 1}"
                            )
                            prepared = await asyncio.wait_for(
                                asyncio.to_thread(
                                    prepare_session_operation_item,
                                    prepared_input,
                                    session_name=task.session_name,
                                    item_index=idx * max(1, len(variants)) + group_position,
                                    total_items=total_group_items,
                                ),
                                timeout=float(task.options.get("video_prepare_timeout_s") or 300),
                            )
                            prepared["id"] = group_item.get("id") or prepared["id"]
                            group_item.clear()
                            group_item.update(prepared)
                            group_item.pop("prepare_error", None)
                            image_counts.append({
                                "group_id": variant.get("group_id"),
                                "group_name": variant.get("group_name"),
                                "status": "ready",
                                "count": len(group_item.get("frames") or []),
                            })
                        except Exception as exc:
                            group_item["prepare_error"] = f"{type(exc).__name__}: {exc}"
                            image_counts.append({
                                "group_id": variant.get("group_id"),
                                "group_name": variant.get("group_name"),
                                "status": "error",
                                "count": 0,
                                "error": group_item["prepare_error"],
                            })
                            log_event(
                                "视频准备",
                                f"失败：{variant.get('group_name') or variant.get('group_id')}",
                                level=logging.ERROR,
                                details=error_details(exc),
                                progress=10,
                                progress_message="部分实验组视频准备失败",
                            )
                    item_dict["image_input"] = {
                        "total_images": sum(row["count"] for row in image_counts),
                        "groups": image_counts,
                    }
                    _persist_task(task, force=True)
                    ready_count = sum(row["status"] == "ready" for row in image_counts)
                    if ready_count == 0:
                        last_error = ValueError("所有实验组视频均无法完成抽帧")
                    else:
                        log_event(
                            "视频准备",
                            "多组关键帧提取完成",
                            details={
                                "可评实验组": ready_count,
                                "输入图片总数": item_dict["image_input"]["total_images"],
                                "各组图片数": image_counts,
                            },
                            progress=12,
                            progress_message=(
                                f"多组关键帧准备完成（共 {item_dict['image_input']['total_images']} 张）"
                            ),
                        )
                elif task.mode in ("operation", "rich_content", "rich_content_quality") and not item_dict.get("frames"):
                    try:
                        log_event(
                            "视频准备",
                            "校验视频并分析场景",
                            details={"视频路径": item_dict.get("video_path")},
                            progress=3,
                            progress_message="正在校验视频并分析场景",
                        )
                        if task.mode in ("rich_content", "rich_content_quality"):
                            if rich_profile is None:
                                raise ValueError("缺少 rich_content 视觉模式配置")
                            prepare_call = prepare_session_rich_content_item
                            prepare_kwargs = {"profile": rich_profile}
                        else:
                            prepare_call = prepare_session_operation_item
                            prepare_kwargs = {}
                        prepared = await asyncio.wait_for(
                            asyncio.to_thread(
                                prepare_call,
                                item_dict,
                                session_name=task.session_name,
                                item_index=idx,
                                total_items=len(task.items),
                                **prepare_kwargs,
                            ),
                            timeout=float(task.options.get("video_prepare_timeout_s") or 300),
                        )
                        item_dict.clear()
                        item_dict.update(prepared)
                        _persist_task(task)
                        video_prepare_warnings = item_dict.get("video_prepare_warnings") or []
                        if video_prepare_warnings:
                            log_event(
                                "视频准备",
                                "时间参数异常，已回退默认值",
                                level=logging.WARNING,
                                details={"警告": "；".join(video_prepare_warnings)},
                                progress=4,
                                progress_message="时间参数异常，已使用默认值完成抽帧",
                            )
                        log_event(
                            "视频准备",
                            "关键帧提取完成",
                            details={
                                "关键帧数": item_dict.get("frame_count"),
                                "抽帧目录": str(Path(item_dict["frames"][0]).parent),
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
                    for attempt in range(2):
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
                                    task.mode, idx, item_dict, rubrics, pair_judges, cfg, scale,
                                    online_runner, process_dims, arbitrator,
                                    rich_judges=rich_judges,
                                    visual_judge=visual_judge,
                                    task=task,
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
                                await asyncio.sleep(1.0)
                                continue
                            break
                if res is None:
                    error_result = {
                        "index": idx,
                        "item_id": item_id,
                        "query": item_dict.get("query", ""),
                        "error": f"{type(last_error).__name__}: {last_error}",
                    }
                    if task.mode == "operation" and _is_multi_group_operation(item_dict):
                        error_result.update(
                            _operation_group_failure_result(item_dict, last_error)
                        )
                    res = error_result
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
            res["duration_s"] = round(time.perf_counter() - started, 1)
            res["judge_provider"] = run_provider_name
            res["judge_provider_id"] = run_provider_id
            res["judge_model"] = run_model
            res["judge_provider_revision"] = run_provider_revision
            for group_result in res.get("group_results") or []:
                group_result["judge_provider"] = run_provider_name
                group_result["judge_provider_id"] = run_provider_id
                group_result["judge_model"] = run_model
                group_result["judge_provider_revision"] = run_provider_revision
            if rerun is not None:
                prior = next(
                    (row for row in task.results if _result_index(row) == idx),
                    None,
                )
                if prior is None:
                    prior = rerun.get("_previous_result")
                res["rerun_count"] = int((prior or {}).get("rerun_count") or 0) + 1
                res["last_rerun_at"] = time.time()
                res["last_rerun_attempt_id"] = attempt_id
            video_prepare_warnings = item_dict.get("video_prepare_warnings") or []
            if video_prepare_warnings:
                res["video_prepare_warnings"] = list(video_prepare_warnings)
            if pending_judge_traces:
                await asyncio.to_thread(
                    flush_web_trace_records,
                    pending_judge_traces,
                    res,
                )
            previous = _upsert_result(task, res)
            if rerun is not None:
                previous = previous or rerun.get("_previous_result")
                rerun["done"] = int(rerun.get("done") or 0) + 1
                rerun.setdefault("items", []).append({
                    "index": idx,
                    "item_id": item_id,
                    "status": "error" if res.get("error") else "done",
                    "error": res.get("error"),
                    "correctness": res.get("correctness"),
                    "total": res.get("total"),
                    "latency_s": res.get("latency_s"),
                    "judge_provider": run_provider_name,
                    "judge_provider_id": run_provider_id,
                    "judge_model": run_model,
                    "judge_provider_revision": run_provider_revision,
                    "previous_status": (
                        "error" if previous and previous.get("error") else
                        "done" if previous else "missing"
                    ),
                    "finished_at": time.time(),
                })
            failed = bool(res.get("error"))
            log_event(
                "任务",
                "完成",
                level=logging.ERROR if failed else logging.INFO,
                details={
                    "状态": "失败" if failed else "成功",
                    "判定": res.get("correctness") or res.get("winner"),
                    "得分": res.get("total"),
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
                    "progress": task.done_total,
                    "total": len(task.items),
                    "result": res,
                    "rerun": rerun is not None,
                    "rerun_progress": (rerun or {}).get("done"),
                    "rerun_total": (rerun or {}).get("total"),
                    "attempt_id": attempt_id or None,
                },
            )
            _persist_task(task)

    indices = list(range(len(task.items))) if item_indices is None else list(item_indices)
    await asyncio.gather(*[one(index, task.items[index]) for index in indices])


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


def _operation_group_result_base(variant: dict) -> dict:
    group_item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
    return {
        "group_id": variant.get("group_id"),
        "group_name": variant.get("group_name"),
        "group_role": variant.get("group_role") or "experiment",
        "dataset_name": variant.get("dataset_name"),
        "availability": variant.get("availability") or ("available" if group_item else "missing"),
        "item_id": group_item.get("id"),
        "query": group_item.get("query"),
        "context": group_item.get("context"),
        "answer": group_item.get("answer"),
        "video_path": group_item.get("video_path"),
        "frame_count": len(group_item.get("frames") or []),
        "submitted_image_count": len(group_item.get("frames") or []),
        "video_prepare_warnings": list(group_item.get("video_prepare_warnings") or []),
    }


def _operation_group_failure_result(item_dict: dict, error: Exception | None) -> dict:
    """构造多组失败结果，保留抽帧、对齐和每组输入等已知信息。"""
    variants = list(item_dict.get("group_variants") or [])
    common_error = f"{type(error).__name__}: {error}" if error else "unknown"
    group_results = []
    for variant in variants:
        row = _operation_group_result_base(variant)
        group_item = variant.get("item")
        if not isinstance(group_item, dict):
            row["evaluation_status"] = "missing_input"
        else:
            row.update(
                evaluation_status="error",
                error=group_item.get("prepare_error") or common_error,
            )
        group_results.append(row)
    image_input = item_dict.get("image_input") or {
        "total_images": sum(row.get("submitted_image_count") or 0 for row in group_results),
        "groups": [
            {
                "group_id": row.get("group_id"),
                "group_name": row.get("group_name"),
                "status": row.get("evaluation_status"),
                "count": row.get("submitted_image_count") or 0,
                **({"error": row.get("error")} if row.get("error") else {}),
            }
            for row in group_results
        ],
    }
    ready_groups = [
        row for row in (image_input.get("groups") or [])
        if row.get("status") == "ready"
    ]
    return {
        "case_id": item_dict.get("case_id") or item_dict.get("id"),
        "alignment_status": item_dict.get("alignment_status"),
        "alignment_warnings": list(item_dict.get("alignment_warnings") or []),
        "requested_evaluation_strategy": item_dict.get("evaluation_strategy") or "",
        "evaluation_strategy": item_dict.get("evaluation_strategy") or "",
        "failure_stage": "evaluation" if ready_groups else "video_prepare",
        "image_input": image_input,
        "input_image_count": image_input.get("total_images") or 0,
        "group_results": group_results,
    }


async def _eval_operation_groups(
    item: EvalItem,
    item_dict: dict,
    rubrics,
    cfg: AppConfig,
) -> dict:
    """执行一个已对齐 case；能联合时一次调用，异常/不一致时按组回退。"""
    variants = list(item_dict.get("group_variants") or [])
    results: dict[str, dict] = {}
    evaluable: list[dict] = []
    for variant in variants:
        group_id = str(variant.get("group_id") or "")
        base = _operation_group_result_base(variant)
        group_item = variant.get("item")
        if not isinstance(group_item, dict):
            base["evaluation_status"] = "missing_input"
            results[group_id] = base
        elif group_item.get("prepare_error"):
            base.update(
                evaluation_status="error",
                error=group_item["prepare_error"],
            )
            results[group_id] = base
        elif not group_item.get("frames"):
            base.update(evaluation_status="error", error="缺少可用关键帧")
            results[group_id] = base
        else:
            evaluable.append(variant)

    op_skill = cfg.domain_skills.get("operation")
    op_dims = op_skill.rubrics if op_skill and op_skill.rubrics else cfg.rubrics
    issue_types = (
        op_skill.operation_policy.issue_types
        if op_skill and op_skill.operation_policy
        else None
    )
    requested_strategy = str(item_dict.get("evaluation_strategy") or "")
    use_joint = (
        requested_strategy.startswith("multi_group") and len(evaluable) >= 2
    )

    if use_joint:
        judge_outputs = await asyncio.gather(
            *[judge.score_operation_groups(item, evaluable) for judge in rubrics]
        )
        for variant in evaluable:
            group_id = str(variant.get("group_id") or "")
            scores = [output[group_id] for output in judge_outputs if group_id in output]
            verdict = aggregate_operation_scores(
                scores,
                op_dims,
                cfg.ensemble,
                cfg.ensemble.flag_low_agreement,
                issue_types,
            )
            row = _operation_group_result_base(variant)
            _fill_operation_verdict(row, verdict)
            row["latency_s"] = round(
                max((score.latency_ms for score in scores), default=0) / 1000,
                1,
            )
            row["evaluation_status"] = "done" if not row.get("error") else "error"
            results[group_id] = row
        actual_strategy = (
            "multi_group" if len(evaluable) == len(variants) else "multi_group_partial"
        )
    else:
        async def score_one(variant: dict) -> tuple[str, dict]:
            group_id = str(variant.get("group_id") or "")
            group_item = variant["item"]
            row = _operation_group_result_base(variant)
            try:
                eval_item = _to_evalitem(group_item, 0)
                answer = str(group_item.get("answer") or "")
                scores = await asyncio.gather(*[
                    judge.score(eval_item, "answer", answer, eval_mode="operation")
                    for judge in rubrics
                ])
                verdict = aggregate_operation_scores(
                    list(scores),
                    op_dims,
                    cfg.ensemble,
                    cfg.ensemble.flag_low_agreement,
                    issue_types,
                )
                _fill_operation_verdict(row, verdict)
                row["latency_s"] = round(
                    max((score.latency_ms for score in scores), default=0) / 1000,
                    1,
                )
                row["evaluation_status"] = "done" if not row.get("error") else "error"
            except Exception as exc:
                row.update(
                    evaluation_status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            return group_id, row

        for group_id, row in await asyncio.gather(*[score_one(v) for v in evaluable]):
            results[group_id] = row
        actual_strategy = "single_fallback"

    ordered_results = [
        results[str(variant.get("group_id") or "")]
        for variant in variants
        if str(variant.get("group_id") or "") in results
    ]
    done_count = sum(row.get("evaluation_status") == "done" for row in ordered_results)
    if done_count == 0:
        errors = [row.get("error") for row in ordered_results if row.get("error")]
        raise ValueError("；".join(errors) or "没有可用的实验组评估结果")
    image_input = item_dict.get("image_input") or {
        "total_images": sum(row.get("submitted_image_count") or 0 for row in ordered_results),
        "groups": [
            {
                "group_id": row.get("group_id"),
                "group_name": row.get("group_name"),
                "count": row.get("submitted_image_count") or 0,
            }
            for row in ordered_results
        ],
    }
    log_event(
        "结果聚合",
        "多组结果已按组输出",
        details={
            "执行策略": actual_strategy,
            "完成组数": done_count,
            "输入图片总数": image_input.get("total_images"),
        },
        progress=90,
        progress_message="正在整理多组对照结果",
    )
    return {
        "case_id": item_dict.get("case_id") or item_dict.get("id"),
        "alignment_status": item_dict.get("alignment_status"),
        "alignment_warnings": list(item_dict.get("alignment_warnings") or []),
        "requested_evaluation_strategy": requested_strategy,
        "evaluation_strategy": actual_strategy,
        "image_input": image_input,
        "input_image_count": image_input.get("total_images") or 0,
        "group_results": ordered_results,
    }


async def _eval_one(
    mode,
    idx,
    item_dict,
    rubrics,
    pair_judges,
    cfg,
    scale,
    online_runner,
    process_dims=None,
    arbitrator=None,
    rich_judges=None,
    visual_judge=None,
    task=None,
) -> dict:
    t0 = time.perf_counter()
    item = _to_evalitem(item_dict, idx)
    out: dict = {"query": item.question}
    if item.context:
        out["context"] = item.context

    # 每个 case 仅一次轻量垂域分类（在裁判并发之前完成）
    classify_model = cfg.eval_options.classify_model
    classify_base_url = cfg.eval_options.classify_base_url or (
        cfg.judges[0].base_url if cfg.judges else None)
    _env_key = (os.environ.get(cfg.eval_options.classify_api_key_env or "")
                 if cfg.eval_options.classify_api_key_env else None)
    _judge_key = cfg.judges[0].api_key() if cfg.judges else None
    classify_api_key = _env_key or _judge_key or "EMPTY"  # 绝不为 None
    # 富内容视觉识别不依赖问答垂域分类；数据有明确垂域时直接使用 category，
    # 未提供时保留 default，避免每条视频在视觉调用前再多跑一次分类模型。
    if mode not in ("rich_content", "rich_content_quality") and classify_model and classify_base_url:
        skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
        try:
            await asyncio.wait_for(
                ensure_classified(item, skill_router,
                                  model=classify_model,
                                  base_url=classify_base_url,
                                  api_key=classify_api_key),
                timeout=20.0,
            )
        except Exception:
            pass  # 分类失败不阻断评测

    if mode in ("single", "process"):
        answer = item_dict["answer"]
        out["answer"] = answer
        competitor = item_dict.get("competitor")
        if competitor:
            out["competitor"] = competitor
        if mode == "process":
            out["trace"] = (item.trace or "")[:200]
            eval_mode, dims = "process", (process_dims or cfg.rubrics)
        else:
            eval_mode, dims = "result", cfg.rubrics

        async def _score(r):
            # 产品专家缺竞品 → 跳过该裁判（不参与本题聚合）
            if r.client.cfg.persona == "product_expert" and not competitor:
                return None
            return await r.score(item, "answer", answer, eval_mode=eval_mode,
                                    process_dims=process_dims, competitor=competitor)

        raw = await asyncio.gather(*[_score(r) for r in rubrics])
        scores = [s for s in raw if s is not None]
        v = aggregate_scores(scores, dims, cfg.ensemble, cfg.ensemble.flag_low_agreement)
        log_event(
            "结果聚合",
            "成功",
            details={
                "裁判数": len(scores),
                "判定": v.correctness if v else None,
                "得分": round(v.total, 2) if v else None,
            },
            progress=90,
            progress_message="正在聚合裁判结果",
        )
        # 多裁判分歧 → 主席仲裁（覆盖为主席最终结论）
        if v and v.low_agreement and len(scores) >= 2 and arbitrator:
            try:
                arb = await arbitrator.arbitrate(item, answer, list(scores))
                v.correctness, v.total, v.rubric = arb["correctness"], arb["total"], arb["rubric"]
                v.arbitrated = True
                v.arbitrator_confidence = arb["confidence"]
                v.arbitrator_rationale = arb["rationale"]
                v.rationale = f"[主席仲裁·置信度{arb['confidence']}] {arb['rationale']}"
            except Exception:
                pass
        _fill_verdict(out, v)
        _maybe_meta(out, item, answer, v)

    elif mode == "operation":
        if _is_multi_group_operation(item_dict):
            out.update(
                await _eval_operation_groups(
                    item,
                    item_dict,
                    rubrics,
                    cfg,
                )
            )
        else:
            answer = item_dict.get("answer", "") or ""  # agent 自述（可选，用于「自述×证据」交叉）
            if answer:
                out["answer"] = answer
            out["has_video"] = bool(item_dict.get("media") or item_dict.get("frames"))

            async def _score_op(r):
                return await r.score(item, "answer", answer, eval_mode="operation")

            raw = await asyncio.gather(*[_score_op(r) for r in rubrics])
            scores = [s for s in raw if s is not None]
            op_skill = cfg.domain_skills.get("operation")
            op_dims = op_skill.rubrics if op_skill and op_skill.rubrics else cfg.rubrics
            v = aggregate_operation_scores(
                scores,
                op_dims,
                cfg.ensemble,
                cfg.ensemble.flag_low_agreement,
                op_skill.operation_policy.issue_types
                if op_skill and op_skill.operation_policy
                else None,
            )
            # 多裁判分歧 → 主席仲裁（纯文本，不带帧；兜底）
            if v and v.low_agreement and len(scores) >= 2 and arbitrator:
                try:
                    arb = await arbitrator.arbitrate(
                        item,
                        answer,
                        list(scores),
                        eval_mode="operation",
                        dims=op_dims,
                        policy=op_skill.operation_policy if op_skill else None,
                    )
                    v.correctness, v.total, v.rubric = arb["correctness"], arb["total"], arb["rubric"]
                    v.task_type = arb["task_type"]
                    v.issue_types = arb["issue_types"]
                    v.is_low_level = arb["is_low_level"]
                    v.arbitrated = True
                    v.arbitrator_confidence = arb["confidence"]
                    v.arbitrator_rationale = arb["rationale"]
                    v.rationale = f"[主席仲裁·置信度{arb['confidence']}] {arb['rationale']}"
                except Exception:
                    pass
            _fill_operation_verdict(out, v)

    elif mode == "rich_content":
        if not rich_judges:
            raise ValueError("没有可用的垂域视觉评测裁判")
        frames = [str(path) for path in (item.metadata.get("frames") or [])]
        if not frames:
            raise ValueError("垂域视觉评测缺少关键帧")
        answer_text = str(item_dict.get("answer_text") or "").strip()
        if answer_text:
            out["answer_text"] = answer_text
        out["has_video"] = bool(item_dict.get("media") or frames)
        # 视觉事实列表不做多裁判模糊合并；使用用户选择顺序中的第一位裁判，
        # 保证 presence/count/items 始终来自同一份自洽观察。
        visual = await rich_judges[0].evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            answer_text=answer_text,
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

    elif mode == "rich_content_quality":
        if visual_judge is None:
            raise ValueError("缺少垂域视觉评测识别裁判（请选择 visual_judge）")
        frames = [str(path) for path in (item.metadata.get("frames") or [])]
        if not frames:
            raise ValueError("垂域视觉综合评测缺少关键帧")
        answer_text = str(item_dict.get("answer_text") or "").strip()
        if answer_text:
            out["answer_text"] = answer_text
        out["has_video"] = bool(item_dict.get("media") or frames)

        # 阶段1：视觉识别 — 使用独立 visual_judge
        log_event(
            "综合评测",
            "视觉识别阶段",
            details={"裁判": visual_judge.client.cfg.name, "模型": visual_judge.client.model},
            progress=30,
            progress_message="正在进行垂域视觉评测识别",
        )
        visual = await visual_judge.evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            answer_text=answer_text,
            frames=frames,
        )
        out.update(visual)
        log_event(
            "综合评测",
            "视觉发现已结构化",
            details={
                "挂卡数": out.get("card_count"),
                "Superlink数": out.get("superlink_count"),
                "需复核": out.get("needs_review"),
            },
            progress=50,
            progress_message=f"视觉识别完成（挂卡{out.get('card_count', 0)}张，Superlink{out.get('superlink_count', 0)}个），正在准备回答质量评测",
        )

        # 阶段2：将视觉发现注入 context，跑多裁判 rubric 评测
        visual_context = _format_visual_findings_for_rubric(visual)
        enriched_context = "\n\n".join(
            part for part in [(item.context or "").strip(), visual_context] if part
        )
        # 更新 item.context 供 RubricJudge 使用
        item.context = enriched_context
        enriched_answer = answer_text or "[此回答以视觉内容为主要交付物，纯文本部分为空]"
        out["answer"] = enriched_answer

        async def _score_rcq(r):
            if r.client.cfg.persona == "product_expert":
                return None
            return await r.score(item, "answer", enriched_answer, eval_mode="result")

        raw = await asyncio.gather(*[_score_rcq(r) for r in rubrics])
        scores = [s for s in raw if s is not None]
        v = aggregate_scores(scores, cfg.rubrics, cfg.ensemble, cfg.ensemble.flag_low_agreement)
        log_event(
            "结果聚合",
            "综合评测聚合完成",
            details={
                "裁判数": len(scores),
                "判定": v.correctness if v else None,
                "得分": round(v.total, 2) if v else None,
            },
            progress=90,
            progress_message="正在聚合多裁判综合评测结果",
        )
        # 多裁判分歧 → 主席仲裁
        if v and v.low_agreement and len(scores) >= 2 and arbitrator:
            try:
                arb = await arbitrator.arbitrate(item, enriched_answer, list(scores))
                v.correctness, v.total, v.rubric = arb["correctness"], arb["total"], arb["rubric"]
                v.arbitrated = True
                v.arbitrator_confidence = arb["confidence"]
                v.arbitrator_rationale = arb["rationale"]
                v.rationale = f"[主席仲裁·置信度{arb['confidence']}] {arb['rationale']}"
            except Exception:
                pass
        _fill_verdict(out, v)
        _maybe_meta(out, item, enriched_answer, v)

    elif mode == "compare":
        aa, ab = item_dict["answer_a"], item_dict["answer_b"]
        out["answer_a"], out["answer_b"] = aa, ab
        pairs = []
        for pj in pair_judges:
            pairs.append(await pj.compare_once(item, "A", aa, "B", ab, order="ab"))
            if cfg.eval_options.pairwise_bidirectional:
                pairs.append(await pj.compare_once(item, "A", aa, "B", ab, order="ba"))
        pr = aggregate_pairs(pairs, cfg.ensemble, cfg.ensemble.flag_low_agreement)
        log_event(
            "结果聚合",
            "成功" if pr is not None else "失败",
            level=logging.INFO if pr is not None else logging.ERROR,
            details={"裁判结果数": len(pairs), "胜者": pr.winner if pr else None},
            progress=90,
            progress_message="正在聚合对比结果",
        )
        if pr is None:
            out["error"] = "裁判无成对输出"
        else:
            out.update(
                winner=pr.winner, a_wins=pr.a_wins, b_wins=pr.b_wins, ties=pr.ties,
                bidirectional_consistent=pr.bidirectional_consistent,
                rationale=pr.rationale, low_agreement=pr.low_agreement,
            )

    else:  # online
        with bind_chain_context(module="被测模型", round=0):
            mo = await online_runner.generate_strict(to_prompt(item), item_id=item.id)
        out["generated_answer"] = mo.answer
        out["answer"] = mo.answer
        if mo.error:
            out["gen_error"] = mo.error
        scores = await asyncio.gather(*[r.score(item, "answer", mo.answer) for r in rubrics])
        v = aggregate_scores(list(scores), cfg.rubrics, cfg.ensemble, cfg.ensemble.flag_low_agreement)
        log_event(
            "结果聚合",
            "成功",
            details={"裁判数": len(scores), "判定": v.correctness if v else None},
            progress=90,
            progress_message="正在聚合裁判结果",
        )
        _fill_verdict(out, v)
        _maybe_meta(out, item, mo.answer, v)

    # 评测时实际归属的垂域（未标注 category 时 _classify 已分类）+ 来源标记 + 题号，供按垂域聚合
    out["item_id"] = item.id
    out["category"] = item.category
    router = rubrics[0].skill_router if rubrics else None
    resolved_skill = router.resolve(item) if router else "default"
    out["category_display"] = router.display_of(resolved_skill) if router else "通用"
    if item.metadata.get("category_source"):
        out["category_source"] = item.metadata["category_source"]
    out["latency_s"] = round(time.perf_counter() - t0, 1)  # 该题评测总耗时（秒，含 agent loop 多轮/多裁判/仲裁）
    return out


def _fill_verdict(out: dict, v) -> None:
    if v is None:
        out["error"] = out.get("error", "裁判无输出")
        return
    out["correctness"] = v.correctness
    out["total"] = round(v.total, 2)
    out["rubric"] = {k: round(val, 2) for k, val in v.rubric.items()}
    out["rubric_reasons"] = v.rubric_reasons or {}
    out["error_type"] = v.error_type
    out["is_low_level"] = v.is_low_level
    # 各维度打分理由拼到"理由"末尾，前端"理由"列与导出可直接看到
    _rat = v.rationale or ""
    _reasons = v.rubric_reasons or {}
    if _reasons:
        _suffix = " ｜ ".join(f"{k}：{rv}" for k, rv in _reasons.items())
        out["rationale"] = (_rat + "  ||  " + _suffix) if _rat else _suffix
    else:
        out["rationale"] = _rat
    out["tool_trace"] = v.single_scores[0].tool_trace if v.single_scores else []
    out["used_search"] = any(s.used_search for s in v.single_scores)
    out["truncated"] = any(s.truncated for s in v.single_scores)
    out["low_agreement"] = v.low_agreement
    out["arbitrated"] = v.arbitrated
    out["arbitrator_confidence"] = v.arbitrator_confidence
    out["na_dimensions"] = v.na_dimensions
    out["top_issue_1_dim"] = v.top_issue_1_dim
    out["top_issue_2_dim"] = v.top_issue_2_dim
    out["top_issue_3_dim"] = v.top_issue_3_dim
    out["top_issues_desc"] = v.top_issues_desc


def _fill_operation_verdict(out: dict, v) -> None:
    if v is None:
        out["error"] = out.get("error", "裁判无输出")
        return
    out["task_type"] = v.task_type
    out["correctness"] = v.correctness
    out["total"] = round(v.total, 2) if v.total is not None else None
    out["rubric"] = {key: round(value, 2) for key, value in v.rubric.items()}
    out["rubric_reasons"] = v.rubric_reasons or {}
    out["issue_types"] = v.issue_types
    out["is_low_level"] = v.is_low_level
    out["execution_routes"] = v.execution_routes
    out["route_evidence"] = [item.model_dump(mode="json") for item in v.route_evidence]
    out["route_rationale"] = v.route_rationale
    out["route_status"] = v.route_status
    rationale = v.rationale or ""
    reasons = v.rubric_reasons or {}
    if reasons:
        suffix = " ｜ ".join(f"{key}：{reason}" for key, reason in reasons.items())
        out["rationale"] = f"{rationale}  ||  {suffix}" if rationale else suffix
    else:
        out["rationale"] = rationale
    out["tool_trace"] = v.single_scores[0].tool_trace if v.single_scores else []
    out["used_search"] = any(score.used_search for score in v.single_scores)
    out["truncated"] = any(score.truncated for score in v.single_scores)
    out["low_agreement"] = v.low_agreement
    out["arbitrated"] = v.arbitrated
    out["arbitrator_confidence"] = v.arbitrator_confidence
    out["na_dimensions"] = v.na_dimensions


def _maybe_meta(out: dict, item: EvalItem, answer: str, v) -> None:
    if item.reference and v is not None:
        obj = ground_truth.compute(answer, item.reference)
        out["objective"] = obj
        out["agree"] = (v.correctness == obj["objective_correct"]) if v.correctness != "unclear" else None


def _duration_stats(rows: list[dict], field: str) -> dict:
    values = []
    for row in rows:
        try:
            value = float(row[field])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)
    values.sort()
    if not values:
        return {}

    def percentile(ratio: float) -> float:
        index = round((len(values) - 1) * ratio)
        return round(values[index], 2)

    return {
        "count": len(values),
        "mean_s": round(sum(values) / len(values), 2),
        "p50_s": percentile(0.5),
        "p95_s": percentile(0.95),
        "max_s": round(values[-1], 2),
        "total_s": round(sum(values), 2),
    }


def _summarize(task: Task, cfg: AppConfig) -> dict:
    if task.mode == "rich_content":
        return _summarize_rich_content(task)
    if task.mode == "rich_content_quality":
        return _summarize_rich_content_quality(task, cfg)
    if task.mode == "operation":
        return _summarize_operation(task, cfg)
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    res = task.results
    ok = [r for r in res if "error" not in r]
    judged = [r for r in ok if r.get("correctness") is not None]
    right_count = sum(1 for r in judged if r.get("correctness") == "right")
    problem_count = sum(1 for r in judged if r.get("correctness") != "right")
    summary: dict = {
        "total": len(res),
        "done": len(ok),
        "failed": len(res) - len(ok),
        "mode": task.mode,
    }
    if judged:
        summary["right_count"] = right_count
        summary["problem_count"] = problem_count
        summary["accuracy"] = round(right_count / len(judged), 3)
    if task.mode in ("single", "online", "process", "operation", "rich_content_quality"):
        totals = [r.get("total") for r in ok if r.get("total") is not None]
        if totals:
            summary["mean_total"] = round(sum(totals) / len(totals), 2)
            summary["norm_mean"] = round(sum(totals) / len(totals) / scale, 3)
        has_meta = [r for r in ok if "agree" in r]
        if has_meta:
            agreed = sum(1 for r in has_meta if r.get("agree") is True)
            summary["meta_n"] = len(has_meta)
            summary["judge_accuracy"] = round(agreed / len(has_meta), 3)
    elif task.mode == "compare":
        a = sum(r.get("a_wins", 0) for r in ok)
        b = sum(r.get("b_wins", 0) for r in ok)
        t = sum(r.get("ties", 0) for r in ok)
        tot = a + b + t
        summary["a_winrate"] = round((a + 0.5 * t) / tot, 3) if tot else None
    # 按垂域总览（compare 是两回答对比、无 correctness，不聚合）；失败不拖垮核心 summary
    if task.mode != "compare":
        try:
            summary["by_skill"] = _by_skill(task, cfg)
        except Exception:
            summary["by_skill"] = []
    return summary


def _summarize_operation(task: Task, cfg: AppConfig) -> dict:
    if task.options.get("operation_layout") == "multi_group":
        results = task.results
        group_rows = [
            group
            for case in results
            for group in (case.get("group_results") or [])
        ]
        group_meta = list(task.options.get("operation_groups") or [])
        known_ids = {str(group.get("group_id") or "") for group in group_meta}
        for row in group_rows:
            group_id = str(row.get("group_id") or "")
            if group_id and group_id not in known_ids:
                group_meta.append({
                    "group_id": group_id,
                    "group_name": row.get("group_name") or group_id,
                    "group_role": row.get("group_role") or "experiment",
                    "dataset_name": row.get("dataset_name") or "",
                })
                known_ids.add(group_id)
        group_summaries = []
        for group in group_meta:
            group_id = str(group.get("group_id") or "")
            rows = [row for row in group_rows if str(row.get("group_id") or "") == group_id]
            judged = [row for row in rows if row.get("evaluation_status") == "done"]
            totals = [row["total"] for row in judged if row.get("total") is not None]
            ok_count = sum(row.get("correctness") == "ok" for row in judged)
            group_summaries.append({
                "group_id": group_id,
                "group_name": group.get("group_name") or group_id,
                "group_role": group.get("group_role") or "experiment",
                "dataset_name": group.get("dataset_name") or "",
                "total_cases": len(results),
                "present": sum(row.get("availability") != "missing" for row in rows),
                "evaluated": len(judged),
                "missing": sum(row.get("evaluation_status") == "missing_input" for row in rows),
                "failed": sum(row.get("evaluation_status") == "error" for row in rows),
                "ok_count": ok_count,
                "completion_rate": round(ok_count / len(judged), 3) if judged else None,
                "mean_total": round(sum(totals) / len(totals), 2) if totals else None,
                "correctness_dist": dict(collections.Counter(
                    row.get("correctness") for row in judged if row.get("correctness")
                )),
                "submitted_images": sum(row.get("submitted_image_count") or 0 for row in rows),
                "latency_stats": _duration_stats(judged, "latency_s"),
            })
        completed_cases = [row for row in results if "error" not in row]
        return {
            "total": len(results),
            "done": len(completed_cases),
            "failed": len(results) - len(completed_cases),
            "mode": task.mode,
            "operation_layout": "multi_group",
            "group_summaries": group_summaries,
            "total_group_evaluations": sum(group["evaluated"] for group in group_summaries),
            "total_submitted_images": sum(
                int((row.get("image_input") or {}).get("total_images") or 0)
                for row in results
            ),
            "case_duration_stats": _duration_stats(results, "duration_s"),
        }
    results = task.results
    completed = [row for row in results if "error" not in row]
    judged = [row for row in completed if row.get("correctness") is not None]
    ok_count = sum(row.get("correctness") == "ok" for row in judged)
    totals = [row["total"] for row in judged if row.get("total") is not None]
    scale = (
        cfg.domain_skills["operation"].rubrics[0].scale
        if cfg.domain_skills.get("operation")
        and cfg.domain_skills["operation"].rubrics
        else 5
    )
    statistics = summarize_operation_results(
        results,
        total_cases=len(task.items),
    )
    return {
        "total": len(results),
        "done": len(completed),
        "failed": len(results) - len(completed),
        "mode": task.mode,
        "ok_count": ok_count,
        "problem_count": len(judged) - ok_count,
        "completion_rate": round(ok_count / len(judged), 3) if judged else None,
        "mean_total": round(sum(totals) / len(totals), 2) if totals else None,
        "norm_mean": (
            round(sum(totals) / len(totals) / scale, 3)
            if totals
            else None
        ),
        "correctness_dist": dict(collections.Counter(row["correctness"] for row in judged)),
        "operation_statistics": statistics,
    }


def _summarize_rich_content(task: Task) -> dict:
    """汇总视觉发现与整体评价，不使用问答类 correctness/准确率口径。"""
    results = task.results
    ok = [row for row in results if "error" not in row]
    card_cases = [row for row in ok if row.get("card_presence") == "present"]
    superlink_cases = [
        row for row in ok if row.get("superlink_presence") == "present"
    ]
    complete = [row for row in ok if row.get("answer_coverage") == "complete"]
    suitability_assessed = [
        row for row in card_cases
        if row.get("card_suitability")
        in {"ok", "nok", "suitable", "partially_suitable", "unsuitable"}
    ]
    suitable = [
        row for row in suitability_assessed
        if row.get("card_suitability") in {"ok", "suitable"}
    ]
    solved_ok = [row for row in ok if row.get("problem_solved") == "ok"]
    solved_nok = [row for row in ok if row.get("problem_solved") == "nok"]
    solved_review = [row for row in ok if row.get("problem_solved") == "need_review"]
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
        "card_suitable_count": len(suitable),
        "card_suitable_rate": (
            round(len(suitable) / len(suitability_assessed), 3)
            if suitability_assessed
            else None
        ),
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
        "solved_ok_rate": (
            round(len(solved_ok) / len(ok), 3) if ok else None
        ),
        "by_category": sorted(
            by_category.values(),
            key=lambda entry: (-entry["count"], entry["category"]),
        ),
    }

def _summarize_rich_content_quality(task: Task, cfg: AppConfig) -> dict:
    """综合汇总：垂域视觉评测发现 + 回答质量评测。"""
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    results = task.results
    ok = [row for row in results if "error" not in row]
    judged = [row for row in ok if row.get("correctness") is not None]
    right_count = sum(1 for row in judged if row.get("correctness") == "right")
    problem_count = sum(1 for row in judged if row.get("correctness") != "right")

    # 视觉汇总（复用 _summarize_rich_content 逻辑）
    card_cases = [row for row in ok if row.get("card_presence") == "present"]
    superlink_cases = [row for row in ok if row.get("superlink_presence") == "present"]
    complete = [row for row in ok if row.get("answer_coverage") == "complete"]
    suitable = [row for row in card_cases if row.get("card_suitability") == "suitable"]
    both = [row for row in ok if row.get("card_presence") == "present" and row.get("superlink_presence") == "present"]

    by_category: dict[str, dict] = {}
    for row in ok:
        category = str(row.get("category") or "default")
        entry = by_category.setdefault(category, {
            "category": category,
            "display": row.get("category_display") or category,
            "count": 0,
            "card_cases": 0,
            "superlink_cases": 0,
        })
        entry["count"] += 1
        entry["card_cases"] += int(row.get("card_presence") == "present")
        entry["superlink_cases"] += int(row.get("superlink_presence") == "present")

    summary: dict = {
        "total": len(results),
        "done": len(ok),
        "failed": len(results) - len(ok),
        "mode": task.mode,
        # 视觉指标
        "card_case_count": len(card_cases),
        "card_presence_rate": (round(len(card_cases) / len(ok), 3) if ok else None),
        "card_total": sum(int(row.get("card_count") or 0) for row in ok),
        "card_suitable_count": len(suitable),
        "card_suitable_rate": (round(len(suitable) / len(card_cases), 3) if card_cases else None),
        "superlink_case_count": len(superlink_cases),
        "superlink_presence_rate": (round(len(superlink_cases) / len(ok), 3) if ok else None),
        "superlink_total_observed": sum(int(row.get("superlink_count") or 0) for row in ok),
        "both_count": len(both),
        "needs_review_count": sum(bool(row.get("needs_review")) for row in ok),
        "complete_coverage_count": len(complete),
        "by_category": sorted(
            by_category.values(),
            key=lambda entry: (-entry["count"], entry["category"]),
        ),
    }
    # 质量指标
    if judged:
        summary["right_count"] = right_count
        summary["problem_count"] = problem_count
        summary["accuracy"] = round(right_count / len(judged), 3)
    totals = [row.get("total") for row in ok if row.get("total") is not None]
    if totals:
        summary["mean_total"] = round(sum(totals) / len(totals), 2)
        summary["norm_mean"] = round(sum(totals) / len(totals) / scale, 3)
    has_meta = [row for row in ok if "agree" in row]
    if has_meta:
        agreed = sum(1 for row in has_meta if row.get("agree") is True)
        summary["meta_n"] = len(has_meta)
        summary["judge_accuracy"] = round(agreed / len(has_meta), 3)
    try:
        summary["by_skill"] = _by_skill(task, cfg)
    except Exception:
        summary["by_skill"] = []
    return summary

def _by_skill(task: Task, cfg: AppConfig) -> list[dict]:
    """把 web 的逐题结果桥接到 domain_report，返回垂域总览 overview（每垂域一行）。

    web 的 result 是扁平 dict（非 Verdict 对象），这里按 result 重建 EvalItem/Verdict/MetaResult
    （model 统一为 "answer"），复用 build_domain_report 的垂域分组与聚类逻辑。
    """
    from ..engine import EvalResults
    from ..report.domain_report import build_domain_report
    from ..schema import MetaResult, Verdict

    skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
    items: list[EvalItem] = []
    verdicts: dict[tuple[str, str], Verdict] = {}
    metas: list[MetaResult] = []

    for r in task.results:
        if "error" in r or "correctness" not in r:
            continue
        idx = r.get("index")
        item_dict = task.items[idx] if (idx is not None and idx < len(task.items)) else None
        if not item_dict:
            continue
        iid = item_dict.get("id", f"q{idx}")
        it = EvalItem(
            id=iid,
            question=item_dict.get("query", ""),
            context=r.get("context") or item_dict.get("context"),
            category=r.get("category") or item_dict.get("category", "default"),
            has_ref=bool(item_dict.get("reference")),
            reference=item_dict.get("reference"),
        )
        if r.get("category_source"):
            it.metadata["category_source"] = r["category_source"]
        items.append(it)
        verdicts[(iid, "answer")] = Verdict(
            item_id=iid,
            model="answer",
            rubric={k: float(x) for k, x in (r.get("rubric") or {}).items()},
            na_dimensions=[str(x) for x in (r.get("na_dimensions") or [])],
            total=float(r.get("total") or 0.0),
            correctness=r.get("correctness", "unclear"),
            error_type=r.get("error_type"),
            low_agreement=bool(r.get("low_agreement")),
        )
        if "agree" in r:
            obj = r.get("objective") or {}
            metas.append(MetaResult(
                item_id=iid,
                model="answer",
                has_ref=True,
                category=(it.categories()[0] if it.categories() else "default"),
                objective_correct=obj.get("objective_correct", "na"),
                judge_correctness=r.get("correctness"),
                agree=r.get("agree"),
            ))

    if not items:
        return {"overview": [], "sections": [], "threshold": 2.0}
    results = EvalResults(verdicts=verdicts, pairs={}, metas=metas, focal_model="answer")
    dom = build_domain_report(results, items, {}, cfg, skill_router, task.id)
    c = dom["C"]
    return {
        "overview": c["overview"],
        "sections": c["sections"],
        "threshold": c["dim_problem_threshold"],
    }
