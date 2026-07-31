from auto_eval.config import EnsembleConfig, RubricDim
from auto_eval.judges import aggregate_operation_scores, aggregate_pairs, aggregate_scores
from auto_eval.schema import OperationSingleScore, SinglePair, SingleScore


def test_aggregate_scores_majority_and_agreement():
    dims = [RubricDim(name="准确性", description="d", scale=5)]
    scores = [
        SingleScore(item_id="i", model="m", judge="j1", rubric={"准确性": 5}, total=5, correctness="right"),
        SingleScore(item_id="i", model="m", judge="j2", rubric={"准确性": 4}, total=4, correctness="right"),
        SingleScore(item_id="i", model="m", judge="j3", rubric={"准确性": 2}, total=2, correctness="wrong"),
    ]
    v = aggregate_scores(scores, dims, EnsembleConfig(), 0.6)
    assert v is not None
    assert v.correctness == "right"  # 2 right vs 1 wrong
    assert v.n_judges == 3
    assert 0 < v.judges_agreement <= 1.0
    assert abs(v.rubric["准确性"] - (5 + 4 + 2) / 3) < 1e-6  # trim_mean 对 3 个值不裁剪


def test_aggregate_scores_weighted():
    """weight 应生效：总分按维度权重加权，而非简单平均。"""
    dims = [
        RubricDim(name="准确性", description="d", scale=5, weight=3.0),
        RubricDim(name="安全性", description="d", scale=5, weight=1.0),
    ]
    scores = [
        SingleScore(
            item_id="i", model="m", judge="j1",
            rubric={"准确性": 5, "安全性": 1}, total=3.0, correctness="right",
        ),
    ]
    v = aggregate_scores(scores, dims, EnsembleConfig(), 0.6)
    # 加权 = (5*3 + 1*1) / (3+1) = 4.0；简单平均会是 (5+1)/2 = 3.0
    assert abs(v.total - 4.0) < 1e-6


def test_aggregate_operation_fields_follow_final_correctness():
    dims = [RubricDim(name="操作完成度", description="d", scale=5)]
    scores = [
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j1",
            rubric={"操作完成度": 2},
            total=2,
            task_type="simple",
            correctness="nok",
            issue_types=["最终步骤未执行", "完成证据不足"],
            is_low_level="yes",
        ),
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j2",
            rubric={"操作完成度": 2},
            total=2,
            task_type="simple",
            correctness="nok",
            issue_types=["最终步骤未执行"],
            is_low_level="yes",
        ),
    ]

    verdict = aggregate_operation_scores(scores, dims, EnsembleConfig(), 0.6)

    assert verdict.correctness == "nok"
    assert verdict.issue_types == ["最终步骤未执行", "完成证据不足"]
    assert verdict.is_low_level == "yes"


def test_aggregate_operation_tie_falls_back_to_others():
    dims = [RubricDim(name="操作完成度", description="d", scale=5)]
    scores = [
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j1",
            rubric={"操作完成度": 5},
            total=5,
            task_type="simple",
            correctness="ok",
        ),
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j2",
            rubric={"操作完成度": 1},
            total=1,
            task_type="simple",
            correctness="nok",
            issue_types=["仅文字声称完成"],
            is_low_level="yes",
        ),
    ]

    verdict = aggregate_operation_scores(scores, dims, EnsembleConfig(), 0.6)

    assert verdict.correctness == "others"
    assert verdict.issue_types == ["评测证据冲突"]
    assert verdict.is_low_level == "no"


def test_aggregate_operation_all_na_keeps_total_empty():
    dims = [RubricDim(name="操作完成度", description="d", scale=5)]
    scores = [
        OperationSingleScore(
            item_id="i",
            model="m",
            judge="j1",
            rubric={},
            na_dimensions=["操作完成度"],
            total=None,
            task_type="simple",
            correctness="others",
            issue_types=["视频损坏"],
        )
    ]

    verdict = aggregate_operation_scores(scores, dims, EnsembleConfig(), 0.6)

    assert verdict.total is None
    assert verdict.na_dimensions == ["操作完成度"]


def test_aggregate_pairs_winrate():
    # 3 个已归一化的比较：A 胜 2 次，B 胜 1 次
    pairs = [
        SinglePair(item_id="i", model_a="A", model_b="B", judge="j1", order="ab", winner="a"),
        SinglePair(item_id="i", model_a="A", model_b="B", judge="j2", order="ba", winner="a"),
        SinglePair(item_id="i", model_a="A", model_b="B", judge="j3", order="ab", winner="b"),
    ]
    pr = aggregate_pairs(pairs, EnsembleConfig(), 0.6)
    assert pr is not None
    assert pr.a_wins == 2 and pr.b_wins == 1
    assert pr.winner == "a"
    assert 0.5 < pr.win_rate_a <= 1.0
