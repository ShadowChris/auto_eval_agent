from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_operation_result_table_shows_agent_statement_with_input_fallback() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(
        encoding="utf-8"
    )

    context_position = app_js.index("...contextCols", app_js.index('mode.value === "operation"'))
    answer_position = app_js.index(
        '{ key: "answer", label: "Agent 自述" }', context_position
    )
    verdict_position = app_js.index(
        '{ key: "correctness", label: "完成判定" }', answer_position
    )
    assert context_position < answer_position < verdict_position
    assert "function operationResultAnswer(result)" in app_js
    assert "item?.source_data?.agent_statement" in app_js
    assert (
        'if (c.key === "answer" && mode.value === "operation") '
        "return operationResultAnswer(r);"
    ) in app_js
