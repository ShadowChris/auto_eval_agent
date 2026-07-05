import asyncio
import logging
from datetime import datetime

from auto_eval.observability import (
    bind_chain_context,
    current_context,
    log_event,
    make_request_id,
)


def test_request_id_uses_task_time_short_id_and_query_index():
    created_at = datetime(2026, 7, 5, 0, 32).astimezone().timestamp()
    assert make_request_id(created_at, "3d4a430ee6cc", 0) == "2607050032_3d4a43_q0"


def test_context_is_nested_and_progress_event_contains_chain_fields():
    events = []
    assert current_context().request_id == "-"

    with bind_chain_context(
        request_id="2607050032_3d4a43_q0",
        item_id="q0",
        item_index=0,
        judge="产品专家(judge_3)",
        round=2,
        progress_callback=events.append,
    ):
        log_event(
            "模型裁判",
            "流式调用成功",
            progress=60,
            progress_message="产品专家 · 第2轮 · 模型返回成功",
        )

    assert events == [
        {
            "request_id": "2607050032_3d4a43_q0",
            "item_id": "q0",
            "item_index": 0,
            "status": "running",
            "percent": 60,
            "message": "产品专家 · 第2轮 · 模型返回成功",
            "module": "模型裁判",
            "event": "流式调用成功",
            "level": "info",
            "judge": "产品专家(judge_3)",
            "round": 2,
            "updated_at": events[0]["updated_at"],
        }
    ]
    assert current_context().request_id == "-"


async def test_parallel_judge_contexts_do_not_mix():
    events = []

    async def emit(judge: str, round_no: int, delay: float):
        with bind_chain_context(module="模型裁判", judge=judge, round=round_no):
            await asyncio.sleep(delay)
            log_event(
                "模型裁判",
                "调用失败，准备重试",
                level=logging.WARNING,
                progress=40,
                progress_message="准备重试",
            )
            await asyncio.sleep(delay)
            log_event(
                "模型裁判",
                "重试成功",
                progress=60,
                progress_message="重试成功",
            )

    with bind_chain_context(
        request_id="2607051200_multi_q0",
        item_id="q0",
        item_index=0,
        progress_callback=events.append,
    ):
        await asyncio.gather(
            emit("研发人员(judge_1)", 2, 0.002),
            emit("终端用户(judge_2)", 5, 0.001),
        )

    assert len(events) == 4
    by_judge = {}
    for event in events:
        by_judge.setdefault(event["judge"], []).append(event)
        assert event["request_id"] == "2607051200_multi_q0"
        assert event["item_index"] == 0
    assert {event["round"] for event in by_judge["研发人员(judge_1)"]} == {2}
    assert {event["round"] for event in by_judge["终端用户(judge_2)"]} == {5}
    assert {event["level"] for event in events} == {"warning", "info"}
