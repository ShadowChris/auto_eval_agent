"""历史记录列表：分页展示（前端静态断言）+ 预览列下线（不再生成/外发）。"""
import json
from pathlib import Path

from auto_eval.web.history import _load_meta_row, _snapshot_meta_row


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "src/auto_eval/web/static/app.js"
INDEX_HTML = PROJECT_ROOT / "src/auto_eval/web/static/index.html"


def test_history_table_paginated_and_preview_column_removed() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")

    # 分页：每页 10 条，表格渲染分页切片，翻页条复用统一机制
    assert "const historyPageSize = 10;" in app_js
    assert 'v-for="h in pagedHistoryItems"' in index_html
    assert 'v-if="historyPageCount>1"' in index_html
    assert "setTablePage('history'" in index_html
    assert "jumpTablePage('history')" in index_html
    # 预览列下线
    assert "<th>预览</th>" not in index_html
    assert "{{ h.preview }}" not in index_html


def test_snapshot_meta_row_no_longer_carries_preview(tmp_path) -> None:
    data = {
        "task_id": "t1",
        "created_at": 1700000000,
        "mode": "rich_content",
        "items": [{"query": "第一条query，超长也不会再进摘要"}],
        "results": [],
    }
    row = _snapshot_meta_row(data, tmp_path / "t1.json")
    assert "preview" not in row
    assert row["task_id"] == "t1"
    assert row["total"] == 1


def test_load_meta_row_strips_legacy_preview_from_old_sidecar(tmp_path) -> None:
    """旧侧车里残留的 preview 字段：读取时剔除，不进 API 响应。"""
    snapshot = tmp_path / "t2.json"
    snapshot.write_text(json.dumps({"task_id": "t2"}), encoding="utf-8")
    sidecar = tmp_path / "t2.json.meta.json"
    sidecar.write_text(
        json.dumps({"task_id": "t2", "preview": "旧的第一条query…"}),
        encoding="utf-8",
    )
    row = _load_meta_row(snapshot)
    assert row is not None
    assert "preview" not in row
