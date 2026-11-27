"""Classification is optional across Web/provider/API and CLI evaluation paths."""
import json
from types import SimpleNamespace

import pytest

from auto_eval import engine
from auto_eval.config import AppConfig, DomainSkill, EvalOptions, JudgeConfig, RubricDim, load_config
from auto_eval.judges import rubric_judge
from auto_eval.judges.base import JudgeReply
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.report.domain_report import build_domain_report
from auto_eval.schema import EvalItem
from auto_eval.web import history, runner, server
from auto_eval.web.llm_providers import LLMProviderPayload, LLMProviderStore
from auto_eval.web.operation_runs import evaluation_snapshot, frozen_evaluation_config
from auto_eval.web.tasks import Task


class FakeJudgeClient:
    def __init__(self, cfg, *args, **kwargs):
        self.cfg = cfg
        self.model = cfg.model
        self.persona = "test"
        self.client = object()
        self.calls = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        return JudgeReply(content=json.dumps({
            "rubric": {"准确性": {"total": 5, "reason": "正确"}},
            "total": 5, "correctness": "right", "rationale": "正常评估",
        }))


def _cfg(enabled=True, dedicated=True):
    dim = RubricDim(name="准确性", description="正确", scale=5)
    return AppConfig(
        models=[], judges=[JudgeConfig(name="test", model="judge-model", api_key_value="test-key")],
        rubrics=[dim],
        domain_skills={
            "default": DomainSkill(name="default", rubrics=[dim], rules="default sentinel"),
            "weather": DomainSkill(name="weather", rubrics=[dim], rules="weather sentinel"),
        },
        eval_options=EvalOptions(enable_classify=enabled,
            classify_model="classify-model" if dedicated else None,
            classify_base_url="https://classifier.test/v1"),
    )


@pytest.fixture
def classification_calls(monkeypatch):
    calls = []
    async def classify(item, client, model, skill_router):
        calls.append(model)
        return "weather"
    async def close():
        pass
    monkeypatch.setattr(rubric_judge, "_classify", classify)
    monkeypatch.setattr(rubric_judge, "build_openai_client", lambda **kwargs: SimpleNamespace(close=close))
    return calls


def test_classification_is_enabled_by_default_and_can_be_disabled_in_yaml(tmp_path):
    for name in ("models.yaml", "judges.yaml", "rubrics.yaml"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    assert load_config(tmp_path).eval_options.enable_classify is True
    (tmp_path / "judges.yaml").write_text("eval_options:\n  enable_classify: false\n", encoding="utf-8")
    assert load_config(tmp_path).eval_options.enable_classify is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,dedicated", [(False, True), (False, False), (True, True), (True, False)])
async def test_web_batch_switch_controls_preclassification_and_judge_fallback(
    tmp_path, monkeypatch, classification_calls, enabled, dedicated,
):
    cfg = _cfg(enabled, dedicated)
    monkeypatch.setattr(runner, "JudgeClient", FakeJudgeClient)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    task = Task(id="switch-web", mode="single", items=[{"id": "1", "query": "天气如何", "answer": "晴"}], options={})
    await runner.run_eval(task, cfg)
    assert task.status == "done", task.results
    assert task.results[0]["correctness"] == "right"
    assert classification_calls == (["classify-model" if dedicated else "judge-model"] if enabled else [])
    assert task.results[0]["category"] == ("weather" if enabled else "default")
    assert task.results[0]["category_source"] == ("auto_classified" if enabled else "classification_disabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,dedicated", [(False, True), (False, False), (True, True), (True, False)])
async def test_cli_engine_switch_controls_both_classification_paths(
    tmp_path, monkeypatch, classification_calls, enabled, dedicated,
):
    cfg = _cfg(enabled, dedicated)
    monkeypatch.setattr(engine, "JudgeClient", FakeJudgeClient)
    evaluator = engine.EvalEngine(cfg, tmp_path)
    item = EvalItem(id="1", question="天气如何")
    verdict = await evaluator._rubric_one(item, "answer", "晴")
    assert verdict is not None
    assert verdict.correctness == "right"
    assert classification_calls == (["classify-model" if dedicated else "judge-model"] if enabled else [])
    assert item.metadata["category_source"] == ("auto_classified" if enabled else "classification_disabled")
    assert len(evaluator.clients[0].calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("category,expected", [("weather", "weather sentinel"), ("default", "default sentinel")])
async def test_disabled_classification_still_uses_local_skill_matching(classification_calls, category, expected):
    cfg = _cfg(False)
    client = FakeJudgeClient(cfg.judges[0])
    judge = rubric_judge.RubricJudge(client, cfg.rubrics, SkillRouter(cfg.domain_skills), enable_classify=False)
    score = await judge.score(EvalItem(id="1", question="天气如何", category=category), "answer", "晴")
    assert score.correctness == "right"
    assert expected in client.calls[0][0]
    assert classification_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated", [False, True])
async def test_reenabling_classification_does_not_treat_previous_skip_as_attempt(
    tmp_path, monkeypatch, classification_calls, dedicated,
):
    monkeypatch.setattr(engine, "JudgeClient", FakeJudgeClient)
    item = EvalItem(id="1", question="天气如何")
    disabled = engine.EvalEngine(_cfg(False, dedicated), tmp_path)
    await disabled._rubric_one(item, "answer", "晴")
    assert item.metadata["category_source"] == "classification_disabled"
    assert not classification_calls

    enabled = engine.EvalEngine(_cfg(True, dedicated), tmp_path)
    await enabled._rubric_one(item, "answer", "晴")
    assert classification_calls == ["classify-model" if dedicated else "judge-model"]
    assert item.category == "weather"
    assert item.metadata["category_source"] == "auto_classified"


@pytest.mark.asyncio
async def test_rerun_uses_current_switch_without_reusing_previous_classification(
    tmp_path, monkeypatch, classification_calls,
):
    monkeypatch.setattr(runner, "JudgeClient", FakeJudgeClient)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    task = Task(id="switch-rerun", mode="single", items=[{"id": "1", "query": "天气如何", "answer": "晴"}], options={})
    await runner.run_eval(task, _cfg(True))
    assert classification_calls == ["classify-model"]
    assert task.results[0]["category"] == "weather"

    await runner.run_rerun(task, _cfg(False), [0])
    assert task.rerun_history[-1]["status"] == "done"
    assert classification_calls == ["classify-model"]
    assert task.results[0]["category"] == "default"
    assert task.results[0]["category_source"] == "classification_disabled"
    assert task.results[0]["correctness"] == "right"


def test_frozen_operation_standard_does_not_override_current_classification_switch():
    original = _cfg(True)
    original.domain_skills["operation"] = DomainSkill(name="operation", rules="原有标准")
    current = _cfg(False)
    snapshot = evaluation_snapshot(original)
    restored = frozen_evaluation_config(current, snapshot)
    assert restored.domain_skills["operation"].rules == "原有标准"
    assert restored.eval_options.enable_classify is False


def test_domain_report_distinguishes_disabled_classification_from_fallback():
    cfg = _cfg(False)
    item = EvalItem(id="1", question="天气如何", metadata={"category_source": "classification_disabled"})
    report = build_domain_report(
        engine.EvalResults(focal_model="answer"), [item], {}, cfg, SkillRouter(cfg.domain_skills), "classify-switch",
    )
    generic = next(row for row in report["C"]["sections"] if row["skill"] == "default")
    assert generic["category_source_pct"] == {"classification_disabled": 1.0}
    assert "已关闭 100.0%" in report["C_md"]


def test_custom_provider_does_not_reenable_disabled_classification(tmp_path, monkeypatch):
    cfg = _cfg(False)
    store = LLMProviderStore(tmp_path / "settings")
    store.create(LLMProviderPayload(id="custom", name="Custom", base_url="https://provider.test/v1",
        models=["provider-model"], default_model="provider-model", api_key="test-key"))
    monkeypatch.setattr(server, "_llm_provider_store", lambda: store)
    runtime = server._runtime_config_for_options(cfg, {
        "judge_backend": {"provider_id": "custom", "model": "provider-model"},
    })
    assert runtime.eval_options.classify_model == "provider-model"
    assert runtime.eval_options.enable_classify is False
    assert cfg.eval_options.enable_classify is False
