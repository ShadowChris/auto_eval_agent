from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.web import runner, server
from auto_eval.web.tasks import Task


def _config() -> AppConfig:
    return AppConfig(
        models=[],
        judges=[
            JudgeConfig(
                name="judge_1",
                display="研发人员",
                persona="strict_expert",
                base_url="https://judge-1.test/v1",
                model="model-1",
            ),
            JudgeConfig(
                name="judge_2",
                display="终端用户",
                persona="end_user",
                base_url="https://judge-2.test/v1",
                model="model-2",
            ),
        ],
        rubrics=[],
    )


def test_operation_api_defaults_to_terminal_user_judge() -> None:
    options = server._with_operation_eval_persona(_config(), "operation", {})

    assert options["judges"] == ["judge_2"]
    assert options["concurrency"] == 8


def test_operation_api_ignores_explicit_judge_selection() -> None:
    options = server._with_operation_eval_persona(
        _config(),
        "operation",
        {
            "judges": ["judge_1"],
            "judge_backend": {"provider_id": "dashscope", "model": "qwen"},
        },
    )

    assert options["judges"] == ["judge_2"]
    assert options["judge_backend"] == {
        "provider_id": "dashscope",
        "model": "qwen",
    }


def test_operation_api_keeps_explicit_concurrency() -> None:
    options = server._with_operation_eval_persona(
        _config(),
        "operation",
        {"concurrency": 12},
    )

    assert options["concurrency"] == 12


def test_operation_runner_falls_back_to_terminal_user_for_missing_judges() -> None:
    task = Task(id="operation-default", mode="operation", items=[], options={})

    selected = runner._selected_judge_configs(task, _config())

    assert [judge.name for judge in selected] == ["judge_2"]


def test_operation_runner_falls_back_for_invalid_judge_name() -> None:
    task = Task(
        id="operation-invalid",
        mode="operation",
        items=[],
        options={"judges": ["missing_judge"]},
    )

    selected = runner._selected_judge_configs(task, _config())

    assert [judge.name for judge in selected] == ["judge_2"]


def test_operation_runner_ignores_explicit_non_terminal_judge() -> None:
    task = Task(
        id="operation-explicit",
        mode="operation",
        items=[],
        options={"judges": ["judge_1"]},
    )

    selected = runner._selected_judge_configs(task, _config())

    assert [judge.name for judge in selected] == ["judge_2"]


def test_non_operation_runner_keeps_legacy_first_judge_default() -> None:
    task = Task(id="single-default", mode="single", items=[], options={})

    selected = runner._selected_judge_configs(task, _config())

    assert [judge.name for judge in selected] == ["judge_1"]
