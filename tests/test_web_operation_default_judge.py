from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_operation_defaults_to_end_user_judge() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")

    assert 'targetMode === "operation"' in app_js
    assert 'judge.persona === "end_user"' in app_js
    assert "selectedJudges.value = defaultJudgeSelection(k)" in app_js
    assert "selectedJudges.value = defaultJudgeSelection(mode.value)" in app_js
