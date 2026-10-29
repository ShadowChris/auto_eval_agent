import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from fastapi import HTTPException
import pytest

from auto_eval.config import load_config
from auto_eval.judges.rubric_judge import RubricJudge
from auto_eval.judges.prompts import OPERATION_SYSTEM
from auto_eval.web import history, operation_runs, runner, server, tasks
from auto_eval.web.operation_runs import evaluation_snapshot, freeze_frames, fork_items, verify_frames
from auto_eval.web.server import OperationForkReq
from auto_eval.web.tasks import Task


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(runner, "RUNS_DIR", root)
    monkeypatch.setattr(server, "RUNS_DIR", root)
    monkeypatch.setattr(history, "HISTORY_DIR", root / "web_history")
    monkeypatch.setattr(tasks, "TASKS", {})
    cfg = load_config(Path("config"))
    monkeypatch.setattr(server, "load_config", lambda *_args: cfg)
    monkeypatch.setattr(server, "_normalize_eval_options", lambda config, options: (dict(options), config))
    return root, cfg


def legacy_item(root, identity="a", content=b"old"):
    directory = root / "legacy" / identity
    directory.mkdir(parents=True)
    frame = directory / "kf_001.jpg"
    frame.write_bytes(content)
    (directory / "keyframes.json").write_text(json.dumps({"selected": [{"index": 1, "time": 3.5}]}))
    return {
        "id": identity, "query": "打开设置", "context": "交互发生时间：2024年1月1日",
        "answer": "已打开", "video_path": "/missing/video.mp4", "frames": [str(frame)],
        "source_data": {"标签": "保留", "分享链接": "https://example.test/a"},
    }


def test_archive_is_independent_and_shared_across_evaluations(runtime):
    root, _ = runtime
    original = legacy_item(root)
    before = deepcopy(original)
    a = fork_items({"items": [original]}, [0], reuse_frames=True, runs_dir=root)[0]
    b = fork_items({"items": [original]}, [0], reuse_frames=True, runs_dir=root)[0]
    assert original == before
    assert a["frames"] != original["frames"]
    assert a["frame_set_id"] == b["frame_set_id"]
    assert a["frames"] == b["frames"]
    Path(original["frames"][0]).unlink()
    verify_frames(a, root)
    metadata = json.loads((Path(a["frames"][0]).parent / "keyframes.json").read_text())
    assert metadata["selected"][0]["time"] == 3.5


def test_frame_tampering_or_missing_file_is_not_silently_replaced(runtime):
    root, _ = runtime
    item = legacy_item(root)
    freeze_frames(item, strict=True, runs_dir=root)
    Path(item["frames"][0]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="变化"):
        verify_frames(item, root)
    Path(item["frames"][0]).unlink()
    with pytest.raises(ValueError):
        verify_frames(item, root)


def test_fresh_extraction_always_has_a_new_generation_even_for_identical_images(runtime):
    root, _ = runtime
    a = legacy_item(root)
    b = deepcopy(a)
    freeze_frames(a, runs_dir=root, generation_id="run-a")
    freeze_frames(b, runs_dir=root, generation_id="run-b")
    assert a["frame_set_id"] != b["frame_set_id"]
    assert Path(a["frames"][0]).read_bytes() == Path(b["frames"][0]).read_bytes()


def test_frame_archive_preserves_extraction_evidence_metadata(runtime):
    root, _ = runtime
    item = legacy_item(root)
    original_metadata = {
        "algorithm_version": "test-v1", "video": "https://example.test/a?token=private",
        "config": {"task_start_time": 0}, "effective_task_end_time": 3.5,
        "selected": [{"index": 1, "time": 3.5, "source": "tail", "keep_reason": "final state"}],
    }
    (Path(item["frames"][0]).parent / "keyframes.json").write_text(json.dumps(original_metadata))
    freeze_frames(item, runs_dir=root)
    archived = Path(item["frames"][0]).parent / "keyframes.json"
    saved = json.loads(archived.read_text())
    assert saved["selected"][0]["keep_reason"] == "final state"
    assert saved["algorithm_version"] == "test-v1"
    assert saved["config"] == original_metadata["config"]
    assert "video" not in saved
    saved["selected"][0]["time"] = 100
    archived.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="元数据发生变化"):
        verify_frames(item, root)


@pytest.mark.asyncio
async def test_full_mode_rebuilds_missing_frames_while_evaluate_only_never_does(runtime, monkeypatch):
    root, cfg = runtime
    item = legacy_item(root)
    freeze_frames(item, runs_dir=root)
    old_version = item["frame_set_id"]
    Path(item["frames"][0]).unlink()
    prepared = []

    def prepare(row, **kwargs):
        assert "frame_set_id" not in row
        prepared.append(row)
        rebuilt = legacy_item(root, "rebuilt", b"new frame")
        return {**row, "frames": rebuilt["frames"]}

    async def evaluate(*args, **kwargs):
        return {"correctness": "ok", "category": "operation"}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    task = Task(id="repair-full", mode="operation", items=[item], options={"execution_mode": "full"})
    await runner.run_eval(task, cfg)
    assert len(prepared) == 1
    assert task.items[0]["frame_set_id"] != old_version
    assert task.results[0]["correctness"] == "ok"


def test_extraction_summary_has_no_effect_statistics_after_history_normalization(runtime):
    _, _cfg = runtime
    task = Task(id="pure-extraction", mode="operation", items=[{"id": "a", "query": "q"}], options={"execution_mode": "extract_only"}, status="done")
    task.results = [{"index": 0, "item_id": "a", "evaluation_status": "not_evaluated", "frame_count": 1}]
    payload = history.snapshot_payload(history.task_to_snapshot(task))
    assert "operation_statistics" not in payload["summary"]
    assert payload["status"] == "completed"


def test_unsafe_frame_source_is_rejected(runtime, tmp_path):
    root, _ = runtime
    external = tmp_path / "external.jpg"
    external.write_bytes(b"external")
    with pytest.raises(ValueError, match="运行目录"):
        freeze_frames({"frames": [str(external)]}, strict=True, runs_dir=root)


def test_refresh_clears_only_derived_state_and_keeps_input(runtime):
    root, _ = runtime
    original = legacy_item(root)
    freeze_frames(original, runs_dir=root)
    child = fork_items({"items": [original]}, [0], reuse_frames=False, runs_dir=root)[0]
    assert "frames" not in child
    assert "frame_set_id" not in child
    for field in ("id", "query", "context", "answer", "video_path", "source_data"):
        assert child[field] == original[field]


def test_snapshot_freezes_policy_and_templates_without_credentials(runtime):
    _, cfg = runtime
    cfg.judges[0].api_key_value = "test-secret-do-not-copy"
    snap = evaluation_snapshot(cfg)
    assert set(snap["templates"]) == {"OPERATION_SYSTEM", "OPERATION_USER", "OPERATION_MULTI_USER"}
    assert "test-secret-do-not-copy" not in json.dumps(snap)
    changed = cfg.model_copy(deep=True)
    changed.domain_skills["operation"].rules += "\nnew-test-standard"
    assert evaluation_snapshot(changed)["policy_hash"] != snap["policy_hash"]
    frozen = operation_runs.frozen_evaluation_config(changed, snap)
    assert frozen.domain_skills["operation"] == cfg.domain_skills["operation"]


def test_frozen_templates_are_per_judge_and_do_not_modify_other_modules():
    frozen = RubricJudge(None, [], operation_templates={"OPERATION_SYSTEM": "frozen {{ value }}"})
    normal = RubricJudge(None, [])
    assert frozen.operation_templates["OPERATION_SYSTEM"].render(value="policy") == "frozen policy"
    assert normal.operation_templates["OPERATION_SYSTEM"] is OPERATION_SYSTEM


@pytest.mark.asyncio
async def test_extract_only_has_no_model_client_or_business_verdict(runtime, monkeypatch):
    root, cfg = runtime

    def prepare(item, **kwargs):
        prepared = legacy_item(root, item["id"], b"extracted")
        return {**item, "frames": prepared["frames"], "frame_count": 1}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "JudgeClient", lambda *_args: pytest.fail("只抽帧不能初始化模型客户端"))
    task = Task(id="extract-only", mode="operation", items=[{"id": "one", "query": "打开设置", "video_path": "video.mp4"}], options={"execution_mode": "extract_only"})
    await runner.run_eval(task, cfg)
    assert task.status == "done"
    assert task.results[0]["evaluation_status"] == "not_evaluated"
    assert task.results[0]["frame_count"] == 1
    assert "correctness" not in task.results[0]
    assert task.summary["evaluated"] == 0
    verify_frames(task.items[0], root)
    with pytest.raises(ValueError, match="尚无评估结果"):
        history.operation_comparison_batch(history.task_to_snapshot(task))


@pytest.mark.asyncio
async def test_evaluate_only_missing_frames_never_prepares_or_calls_model(runtime, monkeypatch):
    root, cfg = runtime
    item = legacy_item(root)
    freeze_frames(item, runs_dir=root)
    Path(item["frames"][0]).unlink()
    monkeypatch.setattr(runner, "prepare_session_operation_item", lambda *_args, **_kwargs: pytest.fail("严格评估不允许补抽"))
    monkeypatch.setattr(runner, "_eval_one", lambda *_args, **_kwargs: pytest.fail("缺帧不允许评估"))
    task = Task(id="missing", mode="operation", items=[item], options={"execution_mode": "evaluate_only"})
    await runner.run_eval(task, cfg)
    assert task.results[0].get("error")
    assert "correctness" not in task.results[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,mode", [("reuse_frames", "evaluate_only"), ("refresh_frames", "extract_only"), ("refresh_frames", "full")])
async def test_fork_does_not_mutate_source_and_preserves_time_ids_and_inputs(runtime, monkeypatch, action, mode):
    root, cfg = runtime
    parent = Task(id="source", mode="operation", items=[legacy_item(root)], options={"execution_mode": "full"}, status="done", created_at=1704067200)
    parent.results = [{"index": 0, "item_id": "a", "correctness": "nok", "rationale": "原结果"}]
    parent.note = "原备注"
    tasks.TASKS[parent.id] = parent
    history.save_task(parent)
    original = deepcopy(asdict(parent))
    path = next(history.HISTORY_DIR.glob("*.json"))
    original_bytes = path.read_bytes()
    prepare_calls = []
    observed_times = []

    def prepare(item, **kwargs):
        prepare_calls.append(item)
        assert "frames" not in item
        generated = legacy_item(root, "fresh-" + kwargs["session_name"], b"fresh")
        return {**item, "frames": generated["frames"], "frame_count": 1}

    async def evaluate(*args, **kwargs):
        assert args[2]["source_data"] == parent.items[0]["source_data"]
        verify_frames(args[2], root)
        observed_times.append(kwargs["task"].options["evaluation_timestamp"])
        return {"item_id": "a", "query": "打开设置", "category": "operation", "correctness": "ok", "total": 5}

    monkeypatch.setattr(runner, "prepare_session_operation_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    response = await server.api_operation_fork(parent.id, OperationForkReq(action=action, execution_mode=mode))
    child = tasks.TASKS[response["task_id"]]
    await child.execution
    assert child.id != parent.id
    assert child.items[0]["id"] == "a"
    assert child.items[0]["context"] == parent.items[0]["context"]
    assert child.options["source_task_id"] == parent.id
    assert child.options["evaluation_timestamp"] == parent.created_at
    assert asdict(parent) == original
    assert path.read_bytes() == original_bytes
    assert parent.results[0]["correctness"] == "nok"
    assert len(prepare_calls) == (0 if action == "reuse_frames" else 1)
    assert not child.results[0].get("error"), child.results
    if mode != "extract_only":
        assert child.results[0]["correctness"] == "ok"
        assert observed_times == [parent.created_at]
        assert child.options["evaluation_snapshot"]["policy_hash"] == evaluation_snapshot(cfg)["policy_hash"]
    else:
        assert "correctness" not in child.results[0]


@pytest.mark.asyncio
async def test_fork_preflight_failure_does_not_create_child(runtime):
    root, _ = runtime
    parent = Task(id="source-bad", mode="operation", items=[{"id": "a", "query": "q", "video_path": "missing.mp4"}], options={}, status="done")
    tasks.TASKS[parent.id] = parent
    with pytest.raises(HTTPException) as error:
        await server.api_operation_fork(parent.id, OperationForkReq(action="reuse_frames"))
    assert error.value.status_code == 422
    assert set(tasks.TASKS) == {parent.id}


@pytest.mark.asyncio
async def test_selected_fork_keeps_original_ids_and_source_is_required_idle(runtime):
    root, _ = runtime
    parent = Task(id="selected", mode="operation", items=[legacy_item(root, "first"), legacy_item(root, "second")], options={}, status="running")
    tasks.TASKS[parent.id] = parent
    with pytest.raises(HTTPException) as error:
        await server.api_operation_fork(parent.id, OperationForkReq(action="refresh_frames", execution_mode="extract_only", item_indices=[1]))
    assert error.value.status_code == 409
    result = fork_items({"items": parent.items}, [1], reuse_frames=True, runs_dir=root)
    assert [item["id"] for item in result] == ["second"]
