import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from auto_eval.config import RubricDim, load_config
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.schema import EvalItem
from auto_eval.table_dataset import convert_table
from auto_eval.web import server
from auto_eval.web.history import export_rows, jsonl_export_rows, rows_to_jsonl
from auto_eval.web.operation_media import prepare_operation_query_images
from auto_eval.web.parse_input import parse_jsonl


def _image(path: Path, color: str = "red") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path)
    return path


def test_operation_jsonl_accepts_optional_query_images() -> None:
    content = "\n".join([
        json.dumps({
            "id": "with-image",
            "query": "导航到图片中的地址",
            "query_images": ["data/address.png"],
            "video_path": "data/run.mp4",
        }, ensure_ascii=False),
        json.dumps({
            "id": "without-image",
            "query": "打开设置",
            "video_path": "data/settings.mp4",
        }, ensure_ascii=False),
    ])

    items, errors = parse_jsonl(content, "operation")

    assert not errors
    assert items[0]["query_images"] == ["data/address.png"]
    assert items[0]["source_data"]["query_images"] == ["data/address.png"]
    assert "query_images" not in items[1]


@pytest.mark.parametrize(
    "query_images, expected",
    [
        ("data/a.png", "必须是图片路径字符串数组"),
        ([""], "必须是非空图片路径"),
        (["1.png", "2.png", "3.png", "4.png", "5.png"], "最多支持 4 张"),
    ],
)
def test_operation_jsonl_rejects_invalid_query_images(query_images, expected) -> None:
    content = json.dumps({
        "query": "q",
        "query_images": query_images,
        "video_path": "data/run.mp4",
    }, ensure_ascii=False)

    items, errors = parse_jsonl(content, "operation")

    assert items == []
    assert expected in errors[0]


def test_prepare_query_images_resolves_and_validates_local_files(tmp_path: Path) -> None:
    image = _image(tmp_path / "data" / "address.png")

    prepared = prepare_operation_query_images(
        {"query_images": ["data/address.png"]},
        base_dir=tmp_path,
    )

    assert prepared["query_images"] == [str(image)]

    with pytest.raises(ValueError, match="不支持的用户输入图片格式"):
        bad = tmp_path / "data" / "address.txt"
        bad.write_text("not an image", encoding="utf-8")
        prepare_operation_query_images(
            {"query_images": [str(bad)]},
            base_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_operation_prompt_separates_query_image_from_recording_frames(
    tmp_path: Path,
) -> None:
    query_image = _image(tmp_path / "query.png", "red")
    frame_1 = _image(tmp_path / "kf_001.jpg", "blue")
    frame_2 = _image(tmp_path / "kf_002.jpg", "green")

    class CaptureClient:
        persona = "终端用户"
        cfg = SimpleNamespace(name="judge", persona="end_user")

        def __init__(self):
            self.system = ""
            self.kwargs = {}

        async def complete(self, system, user, **kwargs):
            self.system = system
            self.kwargs = kwargs
            return SimpleNamespace(
                content=(
                    '<analysis>图片与录屏结果一致。</analysis>'
                    '{"task_type":"simple","rubric":{"操作完成度":5,'
                    '"步骤正确性":5},"total":5,"correctness":"ok",'
                    '"issue_types":[],"is_low_level":"no","rationale":"已完成"}'
                ),
                rounds=1,
                used_search=False,
                tool_trace=[],
                search_queries=[],
                truncated=False,
            )

    client = CaptureClient()
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    judge = RubricJudge(
        client,
        [RubricDim(name="最终态正确", description="是否完成", scale=5)],
        skill_router=SkillRouter(config.domain_skills),
    )

    await judge.score(
        EvalItem(
            id="image-op",
            question="导航到图片中的地址",
            query_images=[str(query_image)],
            category="operation",
            metadata={
                "category_source": "dataset",
                "frames": [str(frame_1), str(frame_2)],
            },
        ),
        model_name="agent",
        answer="已开始导航",
        eval_mode="operation",
    )

    parts = client.kwargs["user_content_parts"]
    text_parts = [part["text"] for part in parts if part["type"] == "text"]
    assert "【原始用户输入图片】" in client.system
    assert "原始用户输入图片 1" in text_parts
    assert "录屏关键帧 1" in text_parts
    assert "录屏关键帧 2" in text_parts
    assert client.kwargs["user_image_roles"] == [
        "query_image",
        "recording_frame",
        "recording_frame",
    ]
    assert client.kwargs["user_image_refs"] == [
        str(query_image),
        str(frame_1),
        str(frame_2),
    ]

    await judge.score(
        EvalItem(
            id="plain-op",
            question="打开设置",
            category="operation",
            metadata={
                "category_source": "dataset",
                "frames": [str(frame_1)],
            },
        ),
        model_name="agent",
        answer="已打开设置",
        eval_mode="operation",
    )

    assert "【原始用户输入图片】" not in client.system
    assert "user_content_parts" not in client.kwargs
    assert client.kwargs["user_image_refs"] == [str(frame_1)]


def test_query_image_upload_validates_and_returns_runtime_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "purple").save(buffer, format="PNG")
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")

    with TestClient(server.app) as client:
        response = client.post(
            "/api/upload/query-image",
            files={"file": ("用户图片.png", buffer.getvalue(), "image/png")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "用户图片.png"
    assert payload["width"] == 32
    assert payload["height"] == 24
    assert Path(payload["query_image_path"]).is_file()


def test_result_query_image_preview_returns_inline_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _image(tmp_path / "data" / "original.png", "orange")
    image_2 = _image(tmp_path / "data" / "second.png", "blue")
    snapshot = {
        "task_id": "image-preview-task",
        "mode": "operation",
        "items": [{
            "id": "image_001",
            "query": "识别图片",
            "query_images": [str(image), str(image_2)],
        }],
        "results": [],
    }
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setattr(server, "get_live_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)

    response = server.api_export_item(
        "image-preview-task",
        0,
        "query_image",
        image_index=0,
    )

    assert Path(response.path) == image
    assert response.media_type == "image/png"
    assert "content-disposition" not in response.headers

    second_response = server.api_export_item(
        "image-preview-task",
        0,
        "query_image",
        image_index=1,
    )
    assert Path(second_response.path) == image_2

    with pytest.raises(server.HTTPException, match="没有对应的原输入图片"):
        server.api_export_item(
            "image-preview-task",
            0,
            "query_image",
            image_index=2,
        )


def test_web_exposes_result_query_image_carousel_and_single_image_upload() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")
    html = (root / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/upload/query-image"' in js
    assert "item.query_images = [...it.queryImages]" in js
    assert 'itemArtifactUrl(result, "query_image")' in js
    assert "添加用户输入图片（可选，单图）" in html
    image_input = html.split('accept="image/jpeg,image/png,image/webp"', 1)[1].split(">", 1)[0]
    assert "multiple" not in image_input
    assert 'onQueryImage($event, entry.index)' in image_input
    assert "上一张" in html
    assert "下一张" in html
    assert "moveQueryImage(entry.index,-1)" not in html
    assert "moveResultQueryImagePreview(r,-1)" in html
    assert "moveResultQueryImagePreview(r,1)" in html
    assert 'queryImagePreviewUrl(r,resultQueryImagePreviewIndex)' in html
    assert "resultQueryImagePreviewIndex.value = 0" in js
    assert 'class="query-image-preview-dialog"' in html
    assert "openQueryImagePreview($event,r)" in html
    assert "function openQueryImagePreview(event, result)" in js
    assert 'v-if="resultQueryImagePreviewItemIndex===Number(r.index)"' in html
    assert '@close="resultQueryImagePreviewItemIndex=null"' in html
    assert "原输入图片" in html
    assert 'accept="image/jpeg,image/png,image/webp"' in html


def test_table_import_maps_single_query_image_path(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    source = tmp_path / "cases.csv"
    pd.DataFrame([{
        "序号": "simple_001",
        "query": "导航到图片中的地址",
        "query_image_path": "data/images/address.png",
        "video_path": "data/videos/simple_001.mp4",
    }]).to_csv(source, index=False, encoding="utf-8-sig")

    result = convert_table(source, input_prefix="带图任务", project_root=tmp_path)

    assert result.rows[0]["query_images"] == ["data/images/address.png"]
    assert result.rows[0]["query_image_path"] == "data/images/address.png"


def test_operation_exports_preserve_query_image_paths() -> None:
    snapshot = {
        "task_id": "image-task",
        "mode": "operation",
        "items": [{
            "id": "simple_001",
            "query": "导航到图片中的地址",
            "query_images": ["data/images/address.png"],
            "video_path": "data/videos/simple_001.mp4",
            "source_data": {
                "id": "simple_001",
                "query": "导航到图片中的地址",
                "query_images": ["data/images/address.png"],
                "video_path": "data/videos/simple_001.mp4",
            },
        }],
        "results": [{
            "index": 0,
            "item_id": "simple_001",
            "query": "导航到图片中的地址",
            "query_images": ["data/images/address.png"],
            "query_image_count": 1,
            "correctness": "ok",
            "rubric": {},
            "rubric_reasons": {},
        }],
        "summary": {},
        "options": {},
        "status": "done",
    }

    sheets = export_rows(snapshot, load_config(Path(__file__).resolve().parents[1] / "config"))
    detail = sheets["数据集明细"][0]
    result = sheets["逐题结果"][0]
    jsonl = json.loads(rows_to_jsonl(jsonl_export_rows(snapshot)))

    assert detail["用户输入图片项目相对路径"] == "data/images/address.png"
    assert result["query_images"] == "data/images/address.png"
    assert result["query_image_count"] == 1
    assert jsonl["query_images"] == ["data/images/address.png"]
