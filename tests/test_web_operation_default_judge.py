from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_single_and_operation_default_to_end_user_judge() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")
    index_html = (PROJECT_ROOT / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")

    assert '["single", "operation", "operation_multi_group", "rich_content"].includes(targetMode)' in app_js
    assert 'String(judge.display || "").trim() === "终端用户"' in app_js
    assert 'judge.persona === "end_user"' in app_js
    assert 'if (!["operation", "operation_multi_group"].includes(mode.value)) return judges.value;' in app_js
    assert 'judges: ["operation", "operation_multi_group"].includes(mode.value)' in app_js
    assert 'v-for="j in visibleJudges"' in index_html
    assert ':disabled="mode===\'operation\' || mode===\'operation_multi_group\'"' in index_html
    assert "selectedJudges.value = defaultJudgeSelection(k)" in app_js
    assert "selectedJudges.value = defaultJudgeSelection(mode.value)" in app_js


def test_operation_results_show_issue_types_and_low_level_fields() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")
    index_html = (PROJECT_ROOT / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")
    style_css = (PROJECT_ROOT / "src/auto_eval/web/static/style.css").read_text(encoding="utf-8")

    assert '{ key: "issue_types", label: "问题类型" }' in app_js
    assert '{ key: "is_low_level", label: "是否低级" }' in app_js
    assert '{ key: "execution_routes", label: "执行链路" }' in app_js
    assert 'join("；")' in app_js
    assert 'if (c.key === "is_low_level") return v === "yes" ? "是" : "否";' in app_js
    assert 'ok: "✓ 完成"' in app_js
    assert 'no_support: "⊘ 客观条件不支持"' in app_js
    assert "function resultWarnings(result)" in app_js
    assert 'v-if="resultWarnings(r).length" class="result-warning-detail"' in index_html
    assert "⚠️ 抽帧警告：{{ warning }}" in index_html
    assert "tr.result-warning-detail td" in style_css
