from pathlib import Path

from auto_eval.config import load_config
from auto_eval.judges.prompts import RICH_CONTENT_SYSTEM
from auto_eval.judges.rich_content_judge import rich_content_result_fields
from auto_eval.schema import RichContentObservation
from auto_eval.web.operation_media import prepare_session_rich_content_item
from auto_eval.web.parse_input import parse_jsonl, parse_text
from auto_eval.web.runner import _summarize_rich_content
from auto_eval.web.tasks import Task


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _profile():
    return load_config(PROJECT_ROOT / "config").visual_modes["rich_content"]


def test_rich_content_profile_is_separate_from_domain_skills() -> None:
    config = load_config(PROJECT_ROOT / "config")
    profile = config.visual_modes["rich_content"]

    assert "rich_content" not in config.domain_skills
    assert profile.display == "垂域挂卡 / Superlink"
    assert profile.card_types["music"] == "音乐"
    assert profile.extraction.algorithm_version == "rich-content-v1.0.0"
    assert profile.extraction.max_edge == 1280


def test_rich_content_jsonl_parses_video_manifest() -> None:
    content = "\n".join([
        '{"id":"rich_1","query":"北京明天天气","context":"当前地点北京",'
        '"video_path":"data/weather.mp4","category":"weather",'
        '"answer_text":"北京明天晴","content_start_time":0,"content_end_time":12.5,'
        '"expected_visual":{"card_count":1,"superlink_count":2}}',
        '{"id":"rich_2","question":"周杰伦有哪些歌","video_path":"data/music.mp4"}',
    ])

    items, errors = parse_jsonl(content, "rich_content")

    assert errors == []
    assert items[0] == {
        "query": "北京明天天气",
        "context": "当前地点北京",
        "id": "rich_1",
        "video_path": "data/weather.mp4",
        "source_line": 1,
        "content_start_time": 0.0,
        "content_end_time": 12.5,
        "category": "weather",
        "answer_text": "北京明天晴",
        "expected_visual": {"card_count": 1, "superlink_count": 2},
    }
    assert items[1]["category"] == "default"


def test_rich_content_jsonl_rejects_invalid_rows() -> None:
    content = "\n".join([
        '{"query":"missing video"}',
        '{"query":"bad answer","video_path":"a.mp4","answer_text":42}',
        '{"query":"bad times","video_path":"b.mp4","content_start_time":5,"content_end_time":5}',
    ])

    items, errors = parse_jsonl(content, "rich_content")

    assert items == []
    assert len(errors) == 3
    assert "缺少 video_path" in errors[0]
    assert "answer_text 必须是字符串" in errors[1]
    assert "content_end_time 必须大于 content_start_time" in errors[2]


def test_rich_content_text_input_requires_video_flow() -> None:
    items, errors = parse_text("问题 ||| 回答", "rich_content")

    assert items == []
    assert errors and "导入 JSONL" in errors[0]


def test_rich_content_prompt_defines_visual_counting_contract() -> None:
    profile = _profile()
    prompt = RICH_CONTENT_SYSTEM.render(
        persona="视觉裁判",
        card_types=profile.card_types,
        suitability_anchors=profile.suitability_anchors,
    )

    assert "普通内嵌图片、正文截图和纯文本段落不算挂卡" in prompt
    assert "产品规则保证这类蓝色文字可点击" in prompt
    assert "同一张挂卡或同一处链接" in prompt
    assert "不得按帧数累计数量" in prompt
    assert "不要输出 correctness、rubric 或 total" in prompt


def test_rich_content_result_derives_presence_counts_and_suitability() -> None:
    observation = RichContentObservation.model_validate({
        "answer_coverage": "complete",
        "cards": [{
            "type": "weather",
            "entity": "北京明日天气",
            "visible_content": "晴，26～34℃",
            "answer_position": "回答中部",
            "relation_to_query": "direct",
            "suitability": "suitable",
            "suitability_score": 5,
            "reason": "地点和日期一致",
            "evidence_frames": [2, 3],
            "confidence": 0.96,
        }],
        "superlinks": [{
            "text": "查看未来15天天气",
            "answer_position": "回答底部",
            "surrounding_context": "天气卡片下方",
            "evidence_frames": [3],
            "confidence": 0.9,
        }],
        "needs_review": False,
        "review_reason": "",
        "rationale": "识别到一张合适的天气卡和一个链接",
    })

    result = rich_content_result_fields(observation)

    assert result["card_presence"] == "present"
    assert result["card_count"] == 1
    assert result["card_suitability"] == "suitable"
    assert result["card_suitability_score"] == 5
    assert result["superlink_presence"] == "present"
    assert result["superlink_count"] == 1
    assert result["superlink_count_type"] == "exact"
    assert result["superlink_texts"] == ["查看未来15天天气"]
    assert result["needs_review"] is False
    assert "correctness" not in result


def test_partial_coverage_uses_lower_bound_or_unknown_count() -> None:
    with_link = rich_content_result_fields(RichContentObservation.model_validate({
        "answer_coverage": "partial",
        "cards": [],
        "superlinks": [{"text": "查看详情", "confidence": 0.8}],
    }))
    without_link = rich_content_result_fields(RichContentObservation.model_validate({
        "answer_coverage": "partial",
        "cards": [],
        "superlinks": [],
    }))

    assert with_link["superlink_count"] == 1
    assert with_link["superlink_count_type"] == "lower_bound"
    assert with_link["needs_review"] is True
    assert without_link["superlink_presence"] == "unclear"
    assert without_link["superlink_count"] is None
    assert without_link["superlink_count_type"] == "unknown"


def test_prepare_rich_content_item_uses_profile_and_session_directory(
    tmp_path: Path,
) -> None:
    video = tmp_path / "data" / "answer.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")
    calls = []

    def fake_extract(video_path, output_dir, **kwargs):
        calls.append((video_path, output_dir, kwargs))
        output_dir.mkdir(parents=True, exist_ok=True)
        frame = output_dir / "kf_001.jpg"
        frame.write_bytes(b"frame")
        return [frame]

    prepared = prepare_session_rich_content_item(
        {
            "id": "rich_001",
            "query": "天气",
            "video_path": "data/answer.mp4",
            "content_start_time": 1,
            "content_end_time": 9,
        },
        profile=_profile(),
        session_name="20260723_120000_rich_content_abc",
        item_index=0,
        total_items=3,
        base_dir=tmp_path,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _: 10.0,
        extract_fn=fake_extract,
    )

    assert prepared["frame_count"] == 1
    assert "20260723_120000_rich_content_abc/001_rich_001" in prepared["frames"][0]
    config = calls[0][2]["config"]
    assert config.task_start_time == 1
    assert config.task_end_time == 9
    assert config.max_edge == 1280
    assert calls[0][2]["algorithm_version"] == "rich-content-v1.0.0"


def test_rich_content_summary_uses_presence_and_suitability_not_accuracy() -> None:
    task = Task(
        id="rich-summary",
        mode="rich_content",
        items=[],
        options={},
        results=[
            {
                "category": "weather",
                "category_display": "天气",
                "answer_coverage": "complete",
                "card_presence": "present",
                "card_count": 1,
                "card_suitability": "suitable",
                "superlink_presence": "present",
                "superlink_count": 2,
                "needs_review": False,
            },
            {
                "category": "music",
                "category_display": "音乐",
                "answer_coverage": "complete",
                "card_presence": "absent",
                "card_count": 0,
                "card_suitability": "not_applicable",
                "superlink_presence": "absent",
                "superlink_count": 0,
                "needs_review": False,
            },
        ],
    )

    summary = _summarize_rich_content(task)

    assert summary["card_presence_rate"] == 0.5
    assert summary["card_suitable_rate"] == 1.0
    assert summary["superlink_total_observed"] == 2
    assert summary["both_count"] == 1
    assert summary["neither_count"] == 1
    assert "accuracy" not in summary


def test_rich_content_runner_skips_extra_domain_classification() -> None:
    runner_source = (
        PROJECT_ROOT / "src/auto_eval/web/runner.py"
    ).read_text(encoding="utf-8")

    assert 'mode != "rich_content" and classify_model and classify_base_url' in runner_source


def test_web_exposes_rich_content_mode_and_columns() -> None:
    app_js = (PROJECT_ROOT / "src/auto_eval/web/static/app.js").read_text(
        encoding="utf-8"
    )
    index_html = (
        PROJECT_ROOT / "src/auto_eval/web/static/index.html"
    ).read_text(encoding="utf-8")

    assert '{ key: "rich_content", label: "垂域挂卡 / Superlink" }' in app_js
    assert '{ key: "card_presence", label: "挂卡" }' in app_js
    assert '{ key: "superlink_count", label: "链接数" }' in app_js
    assert '{ key: "needs_review", label: "需人工复核" }' in app_js
    assert "需人工复核 {{ summary.needs_review_count }} 条" in index_html
    assert "mode!=='rich_content'" in index_html
