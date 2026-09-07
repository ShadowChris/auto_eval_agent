"""裁判选择已迁入页首「系统设置」（全局设置），「评估配置」区块删除。

前端静态断言：设置面板承载裁判 chips、旧的任务级 selectedJudges 链路移除、
批量输入预览每页 5 条。
"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "src/auto_eval/web/static/app.js"
INDEX_HTML = PROJECT_ROOT / "src/auto_eval/web/static/index.html"


def test_judge_chips_live_in_global_settings_panel() -> None:
    index_html = INDEX_HTML.read_text(encoding="utf-8")

    # 裁判 chips 绑定全局设置表单，位于系统设置面板内（单题超时行之后）
    settings_start = index_html.index("settings-section")
    judges_row_at = index_html.index('v-model="settingsForm.judges"')
    timeout_row_at = index_html.index('v-model.number="settingsForm.eval_timeout_s"')
    assert settings_start < timeout_row_at < judges_row_at
    assert 'v-for="j in judges" class="chip"' in index_html
    # 摘要行展示当前裁判
    assert "裁判 {{ settingsJudgeDisplay }}" in index_html


def test_per_task_judge_config_section_removed() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")

    assert "评估配置" not in index_html
    assert "selectedJudges" not in app_js
    assert "visibleJudges" not in app_js
    assert "defaultJudgeSelection" not in app_js
    # 提交不再随任务携带裁判（由全局设置管理）
    assert "judges: selectedJudges.value" not in app_js
    # 章节编号连续：① 批量输入 → ② 结果
    assert "<h2>② 结果" in index_html
    assert "<h2>③" not in index_html


def test_batch_input_preview_page_size_is_five() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "const opPageSize = 5;" in app_js
    # 逐题运行进度表与结果表分页不受影响
    assert "const pageSize = 10;" in app_js
