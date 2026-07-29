from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_single_and_operation_default_to_end_user_judge() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")
    index_html = (PROJECT_ROOT / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")

    assert '["single", "operation", "rich_content"].includes(targetMode)' in app_js
    assert 'String(judge.display || "").trim() === "终端用户"' in app_js
    assert 'judge.persona === "end_user"' in app_js
    assert 'if (mode.value !== "operation") return judges.value;' in app_js
    assert 'judges: mode.value === "operation"' in app_js
    assert 'v-for="j in visibleJudges"' in index_html
    assert ':disabled="mode===\'operation\'"' in index_html
    assert "selectedJudges.value = defaultJudgeSelection(k)" in app_js
    assert "selectedJudges.value = defaultJudgeSelection(mode.value)" in app_js


def test_operation_results_show_error_and_low_level_fields() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")

    assert '{ key: "error_type", label: "错误类型" }' in app_js
    assert '{ key: "is_low_level", label: "是否低级" }' in app_js
    assert 'if (c.key === "is_low_level") return v === "yes" ? "是" : "否";' in app_js
    assert 'partial: "◐ 完成但有瑕疵"' in app_js
