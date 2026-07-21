import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_eval.config import RubricDim, load_config
from auto_eval.dataset import to_prompt
from auto_eval.judges.prompts import (
    OPERATION_USER,
    PAIRWISE_USER,
    RUBRIC_USER,
    resolve_prompt_context,
)
from auto_eval.judges.rubric_judge import RubricJudge, _classify
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.schema import EvalItem
from auto_eval.web.history import export_rows
from auto_eval.web.parse_input import parse_jsonl, parse_text
from auto_eval.web.runner import _to_evalitem


TRUSTED_LABEL = "可信背景条件"
ISOLATION_RULE = "两个隔离的信息区"


def test_text_context_is_optional_and_backward_compatible():
    old_items, old_errors = parse_text("中国最长的河流？ ||| 长江", "single")
    assert not old_errors
    assert old_items == [{"query": "中国最长的河流？", "answer": "长江"}]

    items, errors = parse_text(
        "附近有什么餐厅？ ||| @context: 当前时间19:00，地点上海人民广场 ||| 推荐南京大牌档",
        "single",
    )
    assert not errors
    assert items[0]["context"] == "当前时间19:00，地点上海人民广场"
    assert items[0]["answer"] == "推荐南京大牌档"


def test_text_context_works_in_all_text_modes_and_empty_is_ignored():
    samples = {
        "single": "q ||| @context: c ||| a",
        "compare": "q ||| @context: c ||| a ||| b",
        "online": "q ||| @context: c",
        "process": "q ||| @context: c ||| a ||| trace",
    }
    for mode, text in samples.items():
        items, errors = parse_text(text, mode)
        assert not errors, mode
        assert items[0]["context"] == "c", mode

    items, errors = parse_text("q ||| @context: ||| a", "single")
    assert not errors
    assert "context" not in items[0]


def test_jsonl_context_is_optional_and_empty_is_ignored():
    content = "\n".join(
        [
            json.dumps({"query": "q1", "context": "地点：上海", "answer": "a1"}, ensure_ascii=False),
            json.dumps({"query": "q2", "context": "", "answer": "a2"}, ensure_ascii=False),
            json.dumps({"query": "q3", "answer": "a3"}, ensure_ascii=False),
        ]
    )
    items, errors = parse_jsonl(content, "single")
    assert not errors
    assert items[0]["context"] == "地点：上海"
    assert "context" not in items[1]
    assert "context" not in items[2]


def test_context_reaches_model_and_judge_prompts_as_trusted_background():
    item = EvalItem(id="q1", question="附近有什么餐厅？", context="地点：上海人民广场")
    model_prompt = to_prompt(item)
    assert TRUSTED_LABEL in model_prompt
    assert item.context in model_prompt
    assert model_prompt.index(item.context) < model_prompt.index(item.question)

    rubric = RUBRIC_USER.render(
        current_date="2026年7月3日", question=item.question, context=item.context,
        model_name="answer", answer="回答",
    )
    pairwise = PAIRWISE_USER.render(
        question=item.question, context=item.context, answer_a="A", answer_b="B"
    )
    operation = OPERATION_USER.render(
        current_date="2026年7月3日", question=item.question, context=item.context
    )
    for prompt in (rubric, pairwise, operation):
        assert TRUSTED_LABEL in prompt
        assert item.context in prompt
    assert ISOLATION_RULE in rubric
    assert "隔离的信息区" in pairwise
    assert "隔离的信息区" in operation


def test_explicit_context_replaces_default_evaluation_time():
    frozen = datetime(2026, 7, 4, 23, 40, 12, tzinfo=timezone(timedelta(hours=8)))
    context = "当前时间：2024年1月1日"

    resolved = resolve_prompt_context(context, frozen)
    prompt = RUBRIC_USER.render(
        question="今天是星期几？", context=resolved, model_name="answer", answer="星期一"
    )

    assert resolved == context
    assert "2024年1月1日" in prompt
    assert "2026年07月04日" not in prompt
    assert "当前日期：" not in prompt


@pytest.mark.parametrize("context", [None, "", "   "])
def test_empty_context_falls_back_to_frozen_evaluation_time(context):
    frozen = datetime(2026, 7, 4, 23, 40, 12, tzinfo=timezone(timedelta(hours=8)))

    resolved = resolve_prompt_context(context, frozen)
    prompt = RUBRIC_USER.render(
        question="今天是星期几？", context=resolved, model_name="answer", answer="星期六"
    )

    assert resolved == "当前时间：2026年07月04日 23:40:12（时区：UTC+08:00）"
    assert resolved in prompt
    assert TRUSTED_LABEL in prompt


@pytest.mark.asyncio
async def test_operation_without_context_does_not_inject_evaluation_time():
    frozen = datetime(2026, 7, 4, 23, 40, 12, tzinfo=timezone(timedelta(hours=8)))

    class CaptureClient:
        persona = "终端用户"
        cfg = SimpleNamespace(name="judge", persona="end_user")

        def __init__(self):
            self.user_prompt = ""

        async def complete(self, system, user):
            self.user_prompt = user
            return SimpleNamespace(
                content=(
                    '<analysis>画面显示目标状态。</analysis>'
                    '{"rubric":{"最终态正确":5},"total":5,'
                    '"correctness":"right","error_type":null,"rationale":"已完成"}'
                ),
                rounds=1,
                used_search=False,
                tool_trace=[],
                search_queries=[],
                truncated=False,
            )

    client = CaptureClient()
    judge = RubricJudge(
        client,
        [RubricDim(name="最终态正确", description="最终状态是否满足用户需求", scale=5)],
        evaluation_time=frozen,
    )

    await judge.score(
        EvalItem(id="op", question="打开蓝牙"),
        model_name="agent",
        answer="已打开",
        eval_mode="operation",
    )

    assert "当前时间" not in client.user_prompt
    assert "2026年07月04日" not in client.user_prompt
    assert TRUSTED_LABEL not in client.user_prompt


@pytest.mark.asyncio
async def test_calendar_fact_classification_defaults_to_general_not_math():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    router = SkillRouter(load_config(config_dir).domain_skills)

    class Completions:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="通用"))]
            )

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    item = EvalItem(id="calendar", question="今天是星期几？", context="当前时间：2024年1月1日")

    result = await _classify(item, client, "fake-model", router)

    assert result is None
    system = completions.kwargs["messages"][0]["content"]
    user = completions.kwargs["messages"][1]["content"]
    assert "日历事实默认归通用" in system
    assert "才归数学解题" in system
    assert item.context in user


def test_context_reaches_web_item_result_export():
    item = _to_evalitem({"query": "q", "context": "地点：上海"}, 0)
    assert item.context == "地点：上海"

    snapshot = {
        "task_id": "t1",
        "mode": "single",
        "items": [{"query": "q", "context": "地点：上海", "answer": "a"}],
        "results": [{"item_id": "q0", "query": "q", "context": "地点：上海", "answer": "a"}],
        "summary": {},
    }
    rows = export_rows(snapshot)["逐题结果"]
    assert rows[0]["context"] == "地点：上海"
