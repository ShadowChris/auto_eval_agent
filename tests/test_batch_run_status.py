from auto_eval.web.dataset_revision import batch_run_state


def _items(count: int) -> list[dict]:
    return [{"id": f"q{index}"} for index in range(count)]


def test_batch_run_status_uses_success_progress_for_terminal_states() -> None:
    items = _items(3)

    assert batch_run_state("done", items, [
        {"index": 0}, {"index": 1}, {"index": 2},
    ]) == {
        "status": "completed",
        "progress": 3,
        "processed": 3,
        "total": 3,
    }
    assert batch_run_state("done", items, [
        {"index": 0}, {"index": 1, "error": "provider failed"},
    ]) == {
        "status": "partial_completed",
        "progress": 1,
        "processed": 2,
        "total": 3,
    }
    assert batch_run_state("error", items, [
        {"index": 0, "error": "provider failed"},
    ]) == {
        "status": "failed",
        "progress": 0,
        "processed": 1,
        "total": 3,
    }


def test_batch_run_status_prioritizes_running_and_manual_cancellation() -> None:
    items = _items(2)
    one_success = [{"index": 0}]

    assert batch_run_state("running", items, one_success)["status"] == "running"
    cancelled = batch_run_state("cancelled", items, one_success)
    assert cancelled["status"] == "cancelled"
    assert cancelled["progress"] == 1


def test_batch_run_progress_deduplicates_results_and_ignores_excluded_items() -> None:
    items = [
        {"id": "q0"},
        {"id": "q1", "dataset_status": "excluded"},
    ]
    state = batch_run_state("done", items, [
        {"index": 0},
        {"index": 0},
        {"index": 1},
    ])

    assert state == {
        "status": "completed",
        "progress": 1,
        "processed": 1,
        "total": 1,
    }
