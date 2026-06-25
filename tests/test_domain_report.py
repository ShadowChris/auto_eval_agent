from auto_eval.report.domain_report import _dim_problem_dist
from auto_eval.schema import Verdict


def test_dim_problem_dist_keeps_all_ids_for_drilldown():
    verdicts = [
        Verdict(item_id=f"q{i}", model="answer", rubric={"准确性": 1})
        for i in range(40)
    ]
    result = _dim_problem_dist(verdicts, threshold=2, n_items=40, scale=5)

    assert result["准确性"]["count"] == 40
    assert len(result["准确性"]["item_ids"]) == 40
    assert result["准确性"]["rate"] == 1.0
