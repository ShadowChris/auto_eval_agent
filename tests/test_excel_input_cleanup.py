from pathlib import Path
from unittest.mock import Mock

import pytest


pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from auto_eval.table_dataset import _read_input


@pytest.mark.parametrize("outcome", ["success", "empty", "read_error"])
def test_auto_excel_import_releases_file_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
):
    source = tmp_path / "input.xlsx"
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        pd.DataFrame().to_excel(writer, sheet_name="设备原始", index=False)
        rows = [] if outcome == "empty" else [{"index": "001", "query": "打开设置"}]
        pd.DataFrame(rows).to_excel(writer, sheet_name="合并数据", index=False)

    excel_file = pd.ExcelFile
    workbooks = []

    def track_workbook(*args, **kwargs):
        workbook = excel_file(*args, **kwargs)
        monkeypatch.setattr(workbook, "close", Mock(wraps=workbook.close))
        # Keep a reference so garbage collection cannot hide an unclosed file.
        workbooks.append(workbook)
        return workbook

    monkeypatch.setattr(pd, "ExcelFile", track_workbook)
    if outcome == "read_error":
        monkeypatch.setattr(
            pd, "read_excel", Mock(side_effect=ValueError("worksheet read failed"))
        )

    try:
        if outcome == "success":
            frame, sheet = _read_input(source, sheet="auto", encoding="auto")
            assert sheet == "合并数据"
            assert frame.iloc[0]["query"] == "打开设置"
        else:
            message = "非空工作表" if outcome == "empty" else "worksheet read failed"
            with pytest.raises(ValueError, match=message):
                _read_input(source, sheet="auto", encoding="auto")

        assert len(workbooks) == 1
        workbooks[0].close.assert_called_once()
        # Windows refuses deletion if the Excel reader still holds the file.
        source.unlink()
        assert not source.exists()
    finally:
        for workbook in workbooks:
            workbook.close()
