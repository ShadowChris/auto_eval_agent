import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_eval.config import load_config
from auto_eval.judges.base import JudgeReply
from auto_eval.judges.operation_fields import hoist_misnested_operation_fields
from auto_eval.judges.prompts import parse_operation_group_json_loose
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.schema import EvalItem
from auto_eval.web.history import export_rows, jsonl_export_rows, write_frames_zip
from auto_eval.web.operation_groups import align_operation_groups
from auto_eval.web.runner import _duration_stats, _operation_group_failure_result
from auto_eval.web.server import OperationGroupManifest, OperationGroupsAlignReq, api_align_operation_groups


def _item(case_id: str, query: str, item_id: str) -> dict:
    return {
        "id": item_id,
        "query": query,
        "video_path": f"data/{item_id}.mp4",
        "category": "operation",
        "source_data": {
            "id": item_id,
            "case_id": case_id,
            "query": query,
            "video_path": f"data/{item_id}.mp4",
            "custom": item_id,
        },
    }


def test_multi_group_parser_recovers_group_fields_nested_in_rubric() -> None:
    malformed = (
        '{"results":['
        '{"group_id":"a","task_type":"simple","rubric":{'
        '"操作完成度":{"total":2},"步骤正确性":{"total":3},'
        '"total":2.3,"correctness":"others","issue_types":["未展示可验证结果"],'
        '"is_low_level":"no","rationale":"无结果","execution_routes":["jarvis"],'
        '"route_evidence":[],"route_status":"detected"},'
        '{"group_id":"b","task_type":"simple","rubric":{'
        '"操作完成度":{"total":5},"步骤正确性":{"total":5},'
        '"total":5,"correctness":"ok","issue_types":[],"is_low_level":"no",'
        '"rationale":"已完成","execution_routes":["skill"],'
        '"route_evidence":[],"route_status":"detected"}]}'
    )

    parsed = parse_operation_group_json_loose(malformed)

    assert parsed is not None
    rows = [hoist_misnested_operation_fields(row) for row in parsed["results"]]
    assert [row["group_id"] for row in rows] == ["a", "b"]
    assert rows[0]["correctness"] == "others"
    assert rows[0]["rubric"] == {
        "操作完成度": {"total": 2},
        "步骤正确性": {"total": 3},
    }


def test_multi_group_failure_keeps_prepared_image_metadata() -> None:
    case = {
        "case_id": "c1",
        "alignment_status": "complete",
        "evaluation_strategy": "multi_group",
        "image_input": {
            "total_images": 3,
            "groups": [
                {"group_id": "a", "status": "ready", "count": 2},
                {"group_id": "b", "status": "ready", "count": 1},
            ],
        },
        "group_variants": [
            {"group_id": "a", "group_name": "A", "group_role": "control", "item": {"id": "a1", "frames": ["a1.jpg", "a2.jpg"]}},
            {"group_id": "b", "group_name": "B", "group_role": "experiment", "item": {"id": "b1", "frames": ["b1.jpg"]}},
        ],
    }

    result = _operation_group_failure_result(case, RuntimeError("模型输出解析失败"))

    assert result["case_id"] == "c1"
    assert result["evaluation_strategy"] == "multi_group"
    assert result["failure_stage"] == "evaluation"
    assert result["input_image_count"] == 3
    assert [row["submitted_image_count"] for row in result["group_results"]] == [2, 1]
    assert all(row["evaluation_status"] == "error" for row in result["group_results"])
    assert all("模型输出解析失败" in row["error"] for row in result["group_results"])


def test_duration_stats_include_failed_case_timings() -> None:
    stats = _duration_stats(
        [{"duration_s": 10}, {"duration_s": 20}, {"duration_s": 40, "error": "失败"}],
        "duration_s",
    )

    assert stats == {
        "count": 3,
        "mean_s": 23.33,
        "p50_s": 20.0,
        "p95_s": 40.0,
        "max_s": 40.0,
        "total_s": 70.0,
    }


def test_multi_group_prepare_failure_reports_stage_and_keeps_group_error() -> None:
    case = {
        "case_id": "c2",
        "group_variants": [{
            "group_id": "a",
            "group_name": "A",
            "group_role": "control",
            "item": {"id": "a2", "prepare_error": "视频不存在"},
        }],
        "image_input": {
            "total_images": 0,
            "groups": [{"group_id": "a", "status": "error", "count": 0}],
        },
    }

    result = _operation_group_failure_result(case, RuntimeError("所有组准备失败"))

    assert result["failure_stage"] == "video_prepare"
    assert result["input_image_count"] == 0
    assert result["group_results"][0]["error"] == "视频不存在"


def test_align_groups_uses_case_id_and_falls_back_on_query_mismatch() -> None:
    aligned = align_operation_groups([
        {"group_id": "a", "group_name": "实验组数据", "group_role": "control", "items": [
            _item("c1", "打开蓝牙", "a1"),
            _item("c2", "关闭定位", "a2"),
        ]},
        {"group_id": "b", "group_name": "对照组数据", "group_role": "experiment", "items": [
            _item("c1", "打开蓝牙", "b1"),
            _item("c2", "打开定位", "b2"),
            _item("c3", "调大字体", "b3"),
        ]},
    ])

    by_case = {case["case_id"]: case for case in aligned["cases"]}
    assert by_case["c1"]["evaluation_strategy"] == "multi_group"
    assert [variant["group_role"] for variant in by_case["c1"]["group_variants"]] == [
        "control", "experiment",
    ]
    assert by_case["c2"]["evaluation_strategy"] == "single_fallback_query_mismatch"
    assert "Query 不一致" in by_case["c2"]["alignment_warnings"][0]
    assert by_case["c3"]["evaluation_strategy"] == "single_fallback"
    assert [variant["availability"] for variant in by_case["c3"]["group_variants"]] == [
        "missing", "available",
    ]


def test_align_endpoint_reuses_operation_jsonl_parser() -> None:
    def line(group: str) -> str:
        return json.dumps({
            "id": f"{group}_1",
            "case_id": "case_1",
            "query": "打开设置",
            "video_path": f"data/{group}.mp4",
            "extra": group,
        }, ensure_ascii=False)

    response = api_align_operation_groups(OperationGroupsAlignReq(groups=[
        OperationGroupManifest(group_id="a", group_name="A", group_role="control", dataset_name="a.jsonl", jsonl=line("a")),
        OperationGroupManifest(group_id="b", group_name="B", group_role="experiment", dataset_name="b.jsonl", jsonl=line("b")),
    ]))

    assert response["summary"]["complete_cases"] == 1
    assert response["cases"][0]["case_id"] == "case_1"
    assert response["cases"][0]["group_variants"][0]["item"]["source_data"]["extra"] == "a"


@pytest.mark.asyncio
async def test_multi_group_judge_sends_all_images_once_and_returns_group_scores(monkeypatch) -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    operation = config.domain_skills["operation"]

    class FakeClient:
        cfg = SimpleNamespace(name="end_user", persona="end_user")
        persona = "终端用户"
        model = "fake-model"

        def __init__(self):
            self.calls = []

        async def complete(self, system, user, **kwargs):
            self.calls.append((system, user, kwargs))
            return JudgeReply(content=json.dumps({"results": [
                {
                    "group_id": "a", "task_type": "simple", "correctness": "ok",
                    "issue_types": [], "is_low_level": "no", "total": 5,
                    "rubric": {"操作完成度": {"total": 5}, "步骤正确性": {"total": 5}},
                    "execution_routes": ["fast_system"], "route_status": "detected",
                    "route_evidence": [], "rationale": "A 完成",
                },
                {
                    "group_id": "b", "task_type": "simple", "correctness": "nok",
                    "issue_types": ["应执行目标未执行"], "is_low_level": "yes", "total": 2,
                    "rubric": {"操作完成度": {"total": 1}, "步骤正确性": {"total": 3}},
                    "execution_routes": ["skill"], "route_status": "detected",
                    "route_evidence": [], "rationale": "B 未完成",
                },
            ]}, ensure_ascii=False))

    monkeypatch.setattr("auto_eval.judges.rubric_judge.encode_frame", lambda path: f"image:{path.name}")
    client = FakeClient()
    judge = RubricJudge(
        client,
        config.rubrics,
        SimpleNamespace(domain={"operation": operation}),
        expert_knowledge=config.expert_knowledge["operation"],
    )
    scores = await judge.score_operation_groups(
        EvalItem(id="case_1", question="打开蓝牙", category="operation"),
        [
            {"group_id": "a", "group_name": "A", "item": {"id": "a1", "frames": ["a1.jpg", "a2.jpg"]}},
            {"group_id": "b", "group_name": "B", "item": {"id": "b1", "frames": ["b1.jpg"]}},
        ],
    )

    assert len(client.calls) == 1
    assert len(client.calls[0][2]["user_images"]) == 3
    assert "录屏Query无法与输入Query一致核验" in client.calls[0][0]
    assert "不得比较优劣、输出排名" in client.calls[0][0]
    assert "1. 【统一完成条件】" in client.calls[0][0]
    assert "不等于结果已经生效" in client.calls[0][0]
    assert "2. 【逐组目标状态】" in client.calls[0][0]
    assert "3. 【逐组最终映射】" in client.calls[0][0]
    assert "不得合并组间证据" in client.calls[0][0]
    assert "身份验证或敏感操作环节，不得判 ok" in client.calls[0][0]
    assert "【实验组 组1】" in client.calls[0][1]
    assert "【实验组 A】" not in client.calls[0][1]
    assert "【实验组 B】" not in client.calls[0][1]
    assert scores["a"].correctness == "ok"
    assert scores["b"].correctness == "nok"
    assert scores["b"].issue_types == ["应执行目标未执行"]


def test_multi_group_exports_horizontal_and_long_views(tmp_path: Path) -> None:
    aligned = align_operation_groups([
        {"group_id": "a", "group_name": "A数据", "group_role": "control", "dataset_name": "a.jsonl", "items": [_item("c1", "打开蓝牙", "a1")]},
        {"group_id": "b", "group_name": "B数据", "group_role": "experiment", "dataset_name": "b.jsonl", "items": [_item("c1", "打开蓝牙", "b1")]},
    ])
    snapshot = {
        "task_id": "multi-1",
        "mode": "operation",
        "options": {"operation_layout": "multi_group", "operation_groups": aligned["groups"]},
        "items": aligned["cases"],
        "results": [{
            "index": 0,
            "case_id": "c1",
            "query": "打开蓝牙",
            "evaluation_strategy": "multi_group",
            "input_image_count": 5,
            "duration_s": 12.3,
            "group_results": [
                {"group_id": "a", "group_name": "A数据", "group_role": "control", "correctness": "ok", "issue_types": [], "execution_routes": ["fast_system"], "rationale": "完成", "evaluation_status": "done"},
                {"group_id": "b", "group_name": "B数据", "group_role": "experiment", "correctness": "nok", "issue_types": ["应执行目标未执行"], "execution_routes": ["skill"], "rationale": "未完成", "evaluation_status": "done"},
            ],
        }],
        "summary": {},
    }

    sheets = export_rows(snapshot)
    assert len(sheets["数据集明细"]) == 2
    assert len(sheets["逐题结果"]) == 2
    assert sheets["多组对照"][0]["对照组｜A数据_correctness"] == "ok"
    assert sheets["多组对照"][0]["实验组｜B数据_issue_types"] == "应执行目标未执行"
    assert sheets["多组对照"][0]["Case总耗时（秒）"] == 12.3
    assert sheets["逐题结果"][0]["Case总耗时（秒）"] == 12.3
    jsonl_rows = jsonl_export_rows(snapshot)
    assert [row["custom"] for row in jsonl_rows] == ["a1", "b1"]
    assert jsonl_rows[0]["evaluation"]["correctness"] == "ok"
    assert jsonl_rows[0]["eval_run"]["case_duration_s"] == 12.3
    archive = write_frames_zip(snapshot, tmp_path / "frames.zip")
    with zipfile.ZipFile(archive) as zf:
        manifest = [json.loads(line) for line in zf.read("manifest.jsonl").decode().splitlines()]
    assert [row["group_id"] for row in manifest] == ["a", "b"]
