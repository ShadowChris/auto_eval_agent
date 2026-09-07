"""逐题结果（xlsx/CSV 共用行）尾部携带导入原始列：分享链接 / video_url_domain / session_id。"""
from auto_eval.web.history import export_rows, rows_to_csv


def _snapshot(items, results=None, mode="rich_content"):
    return {
        "mode": mode,
        "items": items,
        "results": results or [],
        "summary": {},
        "item_progress": {},
    }


def _item(i, extra_source):
    base = {"id": f"a{i}", "query": f"q{i}", "video_path": "v.mp4"}
    return {**base, "source_data": {**base, **extra_source}}


def test_passthrough_columns_appended_when_present():
    items = [
        _item(0, {"分享链接": "https://s/1", "video_url_domain": "example.com", "session_id": "s-01"}),
        _item(1, {"分享链接": "", "session_id": "s-01"}),  # 缺 video_url_domain 列的行
    ]
    results = [{"index": 0, "item_id": "a0", "query": "q0", "context": "", "category_display": "通用"}]
    rows = export_rows(_snapshot(items, results))["逐题结果"]
    assert len(rows) == 2  # 未评估占位行也在
    # 列存在即输出（任一 item 带该列），逐行按各自 source_data 取值，None 归空串
    assert rows[0]["分享链接"] == "https://s/1"
    assert rows[0]["video_url_domain"] == "example.com"
    assert rows[0]["session_id"] == "s-01"
    assert rows[1]["分享链接"] == ""
    assert rows[1]["video_url_domain"] == ""
    assert rows[1]["session_id"] == "s-01"
    # 追加在固定列尾部，顺序稳定
    assert list(rows[0])[-3:] == ["分享链接", "video_url_domain", "session_id"]


def test_passthrough_columns_absent_when_dataset_lacks_them():
    items = [_item(0, {"备注": "无三列"})]
    rows = export_rows(_snapshot(items))["逐题结果"]
    assert "分享链接" not in rows[0]
    assert "video_url_domain" not in rows[0]
    assert "session_id" not in rows[0]
    assert "备注" not in rows[0]  # 只透传定名的三列，不携带其他任意原始列


def test_passthrough_legacy_flat_items_without_source_data():
    """旧快照 item 无 source_data：从 item 本体回落取值。"""
    items = [{"id": "a0", "query": "q0", "video_path": "v.mp4", "分享链接": "https://s/9"}]
    rows = export_rows(_snapshot(items))["逐题结果"]
    assert rows[0]["分享链接"] == "https://s/9"
    assert "video_url_domain" not in rows[0]


def test_csv_matches_result_sheet_columns():
    items = [_item(0, {"分享链接": "https://s/1", "video_url_domain": "example.com", "session_id": "s-01"})]
    rows = export_rows(_snapshot(items))["逐题结果"]
    header = rows_to_csv(rows).splitlines()[0]
    assert header.endswith("分享链接,video_url_domain,session_id")


def test_compare_mode_rows_also_carry_passthrough():
    items = [
        {**_item(0, {"session_id": "s-01"}),
         "video1": "a.mp4", "video2": "b.mp4", "answer1": "A", "answer2": "B"},
    ]
    results = [{"index": 0, "item_id": "a0", "query": "q0"}]
    rows = export_rows(_snapshot(items, results, mode="compare"))["逐题结果"]
    assert rows[0]["session_id"] == "s-01"
    assert "分享链接" not in rows[0]
