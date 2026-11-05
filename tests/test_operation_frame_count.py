"""Per-item frame totals match the frame manifest, not uploaded user images."""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from auto_eval.web import history


def _snapshot() -> dict:
    return {
        "task_id": "frame-count", "mode": "operation", "status": "done",
        "items": [
            {"id": "a", "query": "相同问题", "video_path": "a.mp4",
             "frames": ["/missing/first.jpg", "/missing/second.jpg"],
             "frame_count": 99, "query_images": ["/input.png"]},
            {"id": "b", "query": "相同问题", "video_path": "b.mp4", "frames": []},
            {"id": "c", "query": "未评估", "video_path": "c.mp4", "frames": ["/missing/third.jpg"]},
        ],
        "results": [
            {"index": 1, "item_id": "b", "error": "download failed"},
            {"index": 0, "item_id": "a", "error": "model failed", "frame_count": 99},
        ],
    }


def test_export_counts_match_manifest_including_failed_and_pending_rows():
    snapshot = _snapshot()
    original = deepcopy(snapshot)
    sheets = history.export_rows(snapshot)
    assert [row["抽帧张数"] for row in sheets["逐题结果"]] == [2, 0, 1]
    for row in sheets["逐题结果"]:
        actual = sum(
            bool(frame["帧序号"])
            for frame in sheets["抽帧清单"]
            if frame["数据集序号"] == row["数据集序号"]
        )
        assert row["抽帧张数"] == actual
    # Missing files are still recorded extracted frames, not zero successful reads.
    assert sheets["抽帧清单"][0]["抽帧状态"] == "文件缺失"
    assert snapshot == original
    with zipfile.ZipFile(BytesIO(history.build_xlsx(snapshot))) as archive:
        xml = "".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.startswith("xl/worksheets/"))
    assert "抽帧张数" in xml


@pytest.mark.parametrize("compact", [False, True])
def test_old_history_exposes_frame_totals_without_mutating_snapshot(compact):
    snapshot = _snapshot()
    original = deepcopy(snapshot)
    payload = history.snapshot_payload(snapshot, compact=compact)
    assert [row["frame_count"] for row in payload["results"]] == [0, 2]
    assert snapshot == original


def test_multi_group_exports_count_each_groups_frames_independently():
    snapshot = _snapshot()
    snapshot["options"] = {"operation_layout": "multi_group"}
    snapshot["items"] = [{"id": "case", "query": "q", "group_variants": [
        {"group_id": "a", "group_name": "对照组", "item": snapshot["items"][0]},
        {"group_id": "b", "group_name": "实验组", "item": snapshot["items"][1]},
    ]}]
    snapshot["results"] = [{"index": 0, "group_results": []}]
    sheets = history.export_rows(snapshot)
    assert [row["抽帧张数"] for row in sheets["逐题结果"]] == [2, 0]
    assert history.snapshot_payload(snapshot, compact=True)["results"][0]["frame_count"] == 2


def test_web_result_table_displays_frame_count_in_both_execution_modes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the result table display test")
    static = Path(__file__).resolve().parents[1] / "src/auto_eval/web/static"
    js = (static / "app.js").read_text(encoding="utf-8")
    columns = js[js.index("    const resultCols = computed("):js.index("    function columnWidth(")]
    cells = js[js.index("    function cell(r, c)"):js.index("    function showCellTooltip(")]
    script = """
    const assert = require('assert').strict;
    const computed = fn => ({get value() { return fn(); }});
    const mode = {value:'operation'}, loadedTaskOptions = {value:{}};
    const results = {value:[]}, rubricDims = {value:[]};
    """ + columns + cells + """
    for (const execution_mode of ['full', 'evaluate_only', 'extract_only']) {
      loadedTaskOptions.value = {execution_mode};
      const cols = resultCols.value.filter(c => c.key === 'frame_count');
      assert.equal(cols.length, 1);
      assert.equal(cols[0].label, '抽帧张数');
      assert.equal(cell({frame_count:0}, cols[0]), 0);
      assert.equal(cell({frame_count:23, query_image_count:4}, cols[0]), 23);
    }
    mode.value = 'single';
    assert(!resultCols.value.some(c => c.key === 'frame_count'));
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
