"""评估执行：分发三种模式 + 并发 + 推 SSE 事件 + 元评测汇总。

复用 auto_eval 核心：RubricJudge / PairwiseJudge / aggregate_* / build_runner / ground_truth。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from ..paths import RUNS_DIR
from ..config import AppConfig
from ..dataset import to_prompt
from ..judges import (Arbitrator, JudgeClient, PairwiseJudge, RubricJudge, SkillRouter,
                        ensure_classified)
from ..judges.ensemble import aggregate_pairs, aggregate_scores
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
from .tasks import Task


logger = logging.getLogger(__name__)
MAX_PROGRESS_EVENTS_PER_ITEM = 100


def _persist_task(task: Task) -> bool:
    """Persist without allowing history I/O to break the evaluation/SSE."""
    try:
        return bool(save_task(task))
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
    task.queue.put_nowait({"event": "item_progress", "data": event_payload})
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
    await task.publish("start", {"total": len(task.items), "mode": task.mode})
    task.status = "running"
    _persist_task(task)
    try:
        await _run(task, cfg)
        task.summary = _summarize(task, cfg)
        task.status = "done"
        await task.publish("done", {"summary": task.summary, "total": len(task.items)})
        _persist_task(task)
    except Exception as e:
        task.status = "error"
        task.error = f"{type(e).__name__}: {e}"
        await task.publish("error", {"message": task.error})
        _persist_task(task)


async def _run(task: Task, cfg: AppConfig) -> None:
    selected = task.options.get("judges") or [cfg.judges[0].name]
    judges_cfg = [j for j in cfg.judges if j.name in selected] or cfg.judges[:1]
    evaluation_time = datetime.fromtimestamp(task.created_at).astimezone()
    _providers = cfg.eval_options.effective_providers()
    clients = [
        JudgeClient(j, _providers, cfg.eval_options.search_topk)
        for j in judges_cfg
    ]
    skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
    rubrics = [
        RubricJudge(c, cfg.rubrics, skill_router, evaluation_time=evaluation_time)
        for c in clients
    ]
    pair_judges = [PairwiseJudge(c, evaluation_time=evaluation_time) for c in clients]
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    sem = asyncio.Semaphore(int(task.options.get("concurrency", 4)))
    eval_timeout = float(task.options.get("eval_timeout_s") or task.options.get("eval_timeout") or 300.0)

    online_runner = None
    if task.mode == "online":
        model_name = task.options.get("model") or cfg.models[0].name
        mc = next((m for m in cfg.models if m.name == model_name), cfg.models[0])
        online_runner = build_runner(mc)
    process_dims = cfg.process_rubrics
    arbitrator = (
        Arbitrator(clients[0], evaluation_time=evaluation_time)
        if len(judges_cfg) >= 2
        else None
    )
    loop = asyncio.get_running_loop()

    async def one(idx: int, item_dict: dict):
        request_id = make_request_id(task.created_at, task.id, idx)

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

        item_id = item_dict.get("id", f"q{idx}")
        with bind_chain_context(
            request_id=request_id,
            item_id=item_id,
            item_index=idx,
            progress_callback=publish_progress,
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
                                online_runner, process_dims, arbitrator, task=task
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
                    res = {
                        "index": idx,
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
            task.results.append(res)
            task.done_total += 1
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
                {"progress": task.done_total, "total": len(task.items), "result": res},
            )
            _persist_task(task)

    await asyncio.gather(*[one(i, it) for i, it in enumerate(task.items)])


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


async def _eval_one(mode, idx, item_dict, rubrics, pair_judges, cfg, scale, online_runner, process_dims=None, arbitrator=None, task=None) -> dict:
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
    if classify_model and classify_base_url:
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
        v = aggregate_scores(scores, op_dims, cfg.ensemble, cfg.ensemble.flag_low_agreement)
        # 多裁判分歧 → 主席仲裁（纯文本，不带帧；兜底）
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


def _maybe_meta(out: dict, item: EvalItem, answer: str, v) -> None:
    if item.reference and v is not None:
        obj = ground_truth.compute(answer, item.reference)
        out["objective"] = obj
        out["agree"] = (v.correctness == obj["objective_correct"]) if v.correctness != "unclear" else None


def _summarize(task: Task, cfg: AppConfig) -> dict:
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
    if task.mode in ("single", "online", "process", "operation"):
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
