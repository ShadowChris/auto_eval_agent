from io import BytesIO
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from auto_eval.web.frame_preview import create_frame_preview_router


def _client(tmp_path, items):
    app = FastAPI()
    app.include_router(create_frame_preview_router(
        lambda task_id: {"items": items} if task_id == "task" else None,
        runs_dir=tmp_path,
    ))
    return TestClient(app)


def _frame(root: Path, name="kf_001.jpg") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    Image.new("RGB", (320, 640), "blue").save(path)
    return path


def test_manifest_supports_old_history_and_timestamps_without_exposing_paths(tmp_path):
    first = _frame(tmp_path)
    second = _frame(tmp_path, "kf_002.jpg")
    (tmp_path / "keyframes.json").write_text(json.dumps({
        "selected": [{"index": 1, "time": 7}, {"index": 2, "time": 9.2}],
    }), encoding="utf-8")
    with _client(tmp_path, [{"id": "001", "query": "q", "frames": [str(first), str(second)]}]) as client:
        response = client.get("/api/eval/task/items/0/frames")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_count"] == 2
        frames = payload["groups"][0]["frames"]
        assert [row["time"] for row in frames] == [7, 9.2]
        assert str(tmp_path) not in response.text
        full = client.get(frames[0]["url"])
        assert full.content == first.read_bytes()
        thumbnail = client.get(frames[0]["url"] + "&thumbnail=true")
        assert thumbnail.status_code == 200
        with Image.open(BytesIO(thumbnail.content)) as image:
            assert max(image.size) <= 144
        assert "private" in thumbnail.headers["cache-control"]


def test_missing_metadata_and_missing_frames_are_readable(tmp_path):
    frame = _frame(tmp_path)
    with _client(tmp_path, [{"frames": [str(frame), str(tmp_path / "missing.jpg")]}, {}]) as client:
        payload = client.get("/api/eval/task/items/0/frames").json()
        assert payload["groups"][0]["frames"][0]["time"] is None
        assert not payload["groups"][0]["frames"][1]["available"]
        assert client.get("/api/eval/task/items/0/frames/1").status_code == 404
        assert client.get("/api/eval/task/items/1/frames").json()["total_count"] == 0


def test_multigroup_preview_does_not_mix_frames(tmp_path):
    first = _frame(tmp_path / "control")
    second = _frame(tmp_path / "experiment")
    item = {"group_variants": [
        {"group_id": "a", "group_name": "对照", "item": {"frames": [str(first)]}},
        {"group_id": "b", "group_name": "实验", "item": {"frames": [str(second)]}},
    ]}
    with _client(tmp_path, [item]) as client:
        payload = client.get("/api/eval/task/items/0/frames").json()
        assert payload["total_count"] == 2
        assert [row["group_name"] for row in payload["groups"]] == ["对照", "实验"]
        assert "group_index=1" in payload["groups"][1]["frames"][0]["url"]
        assert client.get("/api/eval/task/items/0/frames/0?group_index=1").content == second.read_bytes()
        assert client.get("/api/eval/task/items/0/frames/0?group_index=2").status_code == 404


def test_unsafe_symlink_or_outside_frame_is_never_served(tmp_path):
    outside = _frame(tmp_path / "outside")
    root = tmp_path / "runs"
    root.mkdir()
    link = root / "kf_001.jpg"
    link.symlink_to(outside)
    with _client(root, [{"frames": [str(link), str(outside)]}]) as client:
        payload = client.get("/api/eval/task/items/0/frames").json()
        assert all(not row["available"] for row in payload["groups"][0]["frames"])
        assert client.get("/api/eval/task/items/0/frames/0").status_code == 404
        assert client.get("/api/eval/task/items/0/frames/1").status_code == 404


def test_unknown_items_and_invalid_indices_return_errors(tmp_path):
    with _client(tmp_path, [{}]) as client:
        assert client.get("/api/eval/missing/items/0/frames").status_code == 404
        assert client.get("/api/eval/task/items/1/frames").status_code == 404
        assert client.get("/api/eval/task/items/0/frames/-1").status_code == 404
        assert client.get("/api/eval/task/items/0/frames/0?group_index=-1").status_code == 422


def test_preview_component_is_independent_and_lazy():
    root = Path(__file__).resolve().parents[1] / "src/auto_eval/web/static"
    component = (root / "frame_preview.js").read_text(encoding="utf-8")
    assert "async function open()" in component
    assert "AbortController" in component
    assert 'loading="lazy"' in component
    assert 'aria-label="上一帧"' in component
    assert 'aria-label="下一帧"' in component
    assert "download_remote_video" not in component
    assert "frame-preview" in (root / "index.html").read_text(encoding="utf-8")


def test_result_actions_are_button_styled_and_frames_stay_on_one_line():
    root = Path(__file__).resolve().parents[1] / "src/auto_eval/web/static"
    html = (root / "index.html").read_text(encoding="utf-8")
    start = html.index('<table class="grid results compact-results"')
    table = html[start:html.index("</table>", start)]
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    preview = (root / "frame_preview.js").read_text(encoding="utf-8")

    assert 'class="row-export-link"' not in table
    assert 'class="result-action-button" @click="rerunOne(r)"' in table
    assert 'class="row-frame-actions"' in table
    for field in ("video", "frames", "trace", "rerun"):
        assert f"resultActionWidths.{field} + 'px'" in table
        assert f"resultActionWidths.{field}" in js[js.index("const resultTableWidth"):]
    assert "frames: 152" in js
    assert ".row-frame-actions { display: flex;" in css
    assert "flex-wrap: nowrap; gap: 8px;" in css
    assert 'class="result-action-button frame-preview-trigger"' in preview
    trigger = preview.split('class="result-action-button frame-preview-trigger"', 1)[1].split("</button>", 1)[0]
    assert trigger.endswith(">预览")
    assert "knownCount" in trigger  # Accessible count does not change the visible button width.
