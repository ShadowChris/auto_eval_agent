"""多裁判聚合：rubric 去极值均值、分类多数投票、一致率、重复稳定性、双向一致性、Bootstrap CI。"""
from __future__ import annotations

import collections
from typing import Optional

import numpy as np

from ..config import EnsembleConfig, RubricDim
from ..schema import (
    OperationSingleScore,
    OperationVerdict,
    PairResult,
    SinglePair,
    SingleScore,
    Verdict,
)

_rng = np.random.default_rng(20240622)


def _trim_mean(vals: list[float], trim: float = 0.1) -> float:
    if not vals:
        return 0.0
    vs = sorted(vals)
    k = int(len(vs) * trim)
    core = vs[k : len(vs) - k] if len(vs) > 2 else vs
    return float(np.mean(core)) if core else float(np.mean(vs))


def _mean(vals: list[float]) -> float:
    return float(np.mean(vals)) if vals else 0.0


def _majority(items):
    c = collections.Counter(items)
    top = c.most_common(2)
    if not top:
        return None
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None  # 平票 → None
    return top[0][0]


def _bootstrap_mean_ci(values: list[float], n: int = 200, confidence: float = 0.95):
    if len(values) < 2:
        return None
    arr = np.asarray(values, dtype=float)
    means = [float(arr[_rng.integers(0, len(arr), len(arr))].mean()) for _ in range(n)]
    lo = float(np.percentile(means, (1 - confidence) / 2 * 100))
    hi = float(np.percentile(means, (1 + confidence) / 2 * 100))
    return [lo, hi]


def aggregate_scores(
    scores: list[SingleScore],
    dims: list[RubricDim],
    cfg: EnsembleConfig,
    threshold: float,
) -> Optional[Verdict]:
    if not scores:
        return None

    trim = cfg.rubric == "trim_mean"
    # 用裁判实际输出的维度名做聚合（兼容 Skill 专属维度及通用维度），而非硬编码预定义列表
    all_keys = list(dict.fromkeys(k for s in scores for k in s.rubric))
    # 汇总所有裁判的 N/A 维度，取交集（所有裁判都标 N/A 才算该维度对本题不适用）
    all_na_sets = [set(s.na_dimensions) for s in scores]
    na_consensus = list(set.intersection(*all_na_sets)) if all_na_sets else []
    # 有效的维度：排除共识 N/A
    active_keys = [k for k in all_keys if k not in na_consensus]
    dim_weight = {d.name: d.weight for d in dims}  # 通用维度 weight；未知 key 默认 1.0
    rubric_mean: dict[str, float] = {}
    for k in all_keys:
        vs = [s.rubric[k] for s in scores if k in s.rubric]
        rubric_mean[k] = (_trim_mean(vs) if trim else _mean(vs)) if vs else 0.0
    # 总分：按各维度 weight 加权（仅计入有效维度；高权重维度影响更大）
    wsum = sum(dim_weight.get(k, 1.0) for k in active_keys) or 1.0
    total = sum(rubric_mean[k] * dim_weight.get(k, 1.0) for k in active_keys) / wsum if active_keys else 0.0

    correctness = _majority([s.correctness for s in scores]) or "unclear"
    if correctness == "right":
        error_type = None
    else:
        # 只聚合同最终 correctness 一致的错因，平票时确定性回退到第一项。
        ets = [s.error_type for s in scores if s.correctness == correctness and s.error_type]
        error_type = (_majority(ets) or ets[0]) if ets else None
        if error_type is None and any(dim.name == "操作完成度" for dim in dims):
            error_type = "证据冲突" if correctness == "unclear" else "未归因"
    low_level_votes = [
        s.is_low_level
        for s in scores
        if s.correctness == correctness and s.is_low_level in {"yes", "no"}
    ]
    is_low_level = "yes" if low_level_votes.count("yes") > low_level_votes.count("no") else "no"
    if correctness in {"right", "unclear"}:
        is_low_level = "no"

    # 多裁判一致率：correctness 最多类占比
    corr = [s.correctness for s in scores]
    agree = max(collections.Counter(corr).values()) / len(corr) if corr else None

    # 重复稳定性：按裁判分组，求各组 total 的标准差再平均
    by_judge: dict[str, list[float]] = collections.defaultdict(list)
    for s in scores:
        by_judge[s.judge].append(s.total)
    stds = [float(np.std(v)) for v in by_judge.values() if len(v) > 1]
    repeat_std = float(np.mean(stds)) if stds else 0.0

    scale = dims[0].scale if dims else 5
    low = (agree is not None and agree < threshold) or repeat_std > (0.15 * scale + 0.3)

    # 维度打分理由：多裁判合并（每维度各裁判理由拼接，最多 3 条防爆）
    all_reason_keys = list(dict.fromkeys(k for s in scores for k in s.rubric_reasons))
    rubric_reasons: dict[str, str] = {}
    for k in all_reason_keys:
        parts = [f"[{s.judge}] {s.rubric_reasons[k]}" for s in scores if k in s.rubric_reasons]
        if parts:
            rubric_reasons[k] = " | ".join(parts[:3])

    # 取第一个裁判的 top_issue 字段
    first = scores[0]

    return Verdict(
        item_id=scores[0].item_id,
        model=scores[0].model,
        rubric=rubric_mean,
        rubric_reasons=rubric_reasons,
        na_dimensions=na_consensus,
        total=total,
        correctness=correctness,
        error_type=error_type,
        is_low_level=is_low_level,
        rationale=" | ".join(f"[{s.judge}] {s.rationale}" for s in scores[:3]),
        top_issue_1_dim=first.top_issue_1_dim,
        top_issue_2_dim=first.top_issue_2_dim,
        top_issue_3_dim=first.top_issue_3_dim,
        top_issues_desc=first.top_issues_desc,
        n_judges=len(by_judge),
        judges_agreement=agree,
        repeat_std=repeat_std,
        low_agreement=low,
        single_scores=scores,
    )


def aggregate_operation_scores(
    scores: list[OperationSingleScore],
    dims: list[RubricDim],
    cfg: EnsembleConfig,
    threshold: float,
    issue_types_by_status: dict[str, list[str]] | None = None,
) -> Optional[OperationVerdict]:
    """聚合任务类评分，不复用问答类 correctness/error_type 语义。"""
    if not scores:
        return None

    trim = cfg.rubric == "trim_mean"
    all_keys = list(dict.fromkeys(k for score in scores for k in score.rubric))
    all_na_sets = [set(score.na_dimensions) for score in scores]
    na_consensus = list(set.intersection(*all_na_sets)) if all_na_sets else []
    active_keys = [key for key in all_keys if key not in na_consensus]
    dim_weight = {dim.name: dim.weight for dim in dims}
    rubric_mean: dict[str, float] = {}
    for key in all_keys:
        values = [score.rubric[key] for score in scores if key in score.rubric]
        rubric_mean[key] = (_trim_mean(values) if trim else _mean(values)) if values else 0.0
    weight_sum = sum(dim_weight.get(key, 1.0) for key in active_keys)
    total = (
        sum(rubric_mean[key] * dim_weight.get(key, 1.0) for key in active_keys) / weight_sum
        if active_keys and weight_sum
        else None
    )

    correctness = _majority([score.correctness for score in scores]) or "others"
    matching_scores = [score for score in scores if score.correctness == correctness]
    issue_counts: collections.Counter[str] = collections.Counter()
    issue_order: dict[str, int] = {}
    for score in matching_scores:
        for issue in score.issue_types:
            issue_counts[issue] += 1
            issue_order.setdefault(issue, len(issue_order))
    issue_types = sorted(
        issue_counts,
        key=lambda issue: (-issue_counts[issue], issue_order[issue]),
    )
    if issue_types_by_status:
        primary_types = set(issue_types_by_status.get(correctness, []))
        issue_types = (
            [issue for issue in issue_types if issue in primary_types]
            + [issue for issue in issue_types if issue not in primary_types]
        )
    if correctness != "ok" and not issue_types:
        issue_types = ["评测证据冲突" if correctness == "others" else "其他执行问题"]

    task_types = [score.task_type for score in matching_scores if score.task_type]
    task_type = _majority(task_types) or (task_types[0] if task_types else None)
    low_level_votes = [
        score.is_low_level
        for score in matching_scores
        if score.is_low_level in {"yes", "no"}
    ]
    is_low_level = (
        "yes"
        if correctness == "nok"
        and task_type != "complex"
        and low_level_votes.count("yes") > low_level_votes.count("no")
        else "no"
    )

    correctness_votes = [score.correctness for score in scores]
    agreement = (
        max(collections.Counter(correctness_votes).values()) / len(correctness_votes)
        if correctness_votes
        else None
    )
    by_judge: dict[str, list[float]] = collections.defaultdict(list)
    for score in scores:
        if score.total is not None:
            by_judge[score.judge].append(score.total)
    stds = [float(np.std(values)) for values in by_judge.values() if len(values) > 1]
    repeat_std = float(np.mean(stds)) if stds else 0.0
    scale = dims[0].scale if dims else 5
    low_agreement = (
        (agreement is not None and agreement < threshold)
        or repeat_std > (0.15 * scale + 0.3)
    )

    all_reason_keys = list(
        dict.fromkeys(key for score in scores for key in score.rubric_reasons)
    )
    rubric_reasons: dict[str, str] = {}
    for key in all_reason_keys:
        parts = [
            f"[{score.judge}] {score.rubric_reasons[key]}"
            for score in scores
            if key in score.rubric_reasons
        ]
        if parts:
            rubric_reasons[key] = " | ".join(parts[:3])

    return OperationVerdict(
        item_id=scores[0].item_id,
        model=scores[0].model,
        rubric=rubric_mean,
        rubric_reasons=rubric_reasons,
        na_dimensions=na_consensus,
        total=total,
        task_type=task_type,
        correctness=correctness,
        issue_types=issue_types,
        is_low_level=is_low_level,
        rationale=" | ".join(f"[{score.judge}] {score.rationale}" for score in scores[:3]),
        n_judges=len({score.judge for score in scores}),
        judges_agreement=agreement,
        repeat_std=repeat_std,
        low_agreement=low_agreement,
        single_scores=scores,
    )


def aggregate_pairs(
    pairs: list[SinglePair],
    cfg: EnsembleConfig,
    threshold: float,
) -> Optional[PairResult]:
    if not pairs:
        return None
    a = sum(1 for p in pairs if p.winner == "a")
    b = sum(1 for p in pairs if p.winner == "b")
    t = sum(1 for p in pairs if p.winner == "tie")
    n = a + b + t
    winner = "a" if a > b else ("b" if b > a else "tie")
    win_rate_a = (a + 0.5 * t) / n if n else 0.0
    agree = max(a, b, t) / n if n else None

    # 双向一致性：ab / ba 两个方向归一化后的多数胜者应一致
    ab = [p for p in pairs if p.order == "ab"]
    ba = [p for p in pairs if p.order == "ba"]
    bidi = True
    if ab and ba:
        wa = _majority([p.winner for p in ab])
        wb = _majority([p.winner for p in ba])  # 已归一化到固定 model_a/b
        if wa and wb and wa != wb:
            bidi = False

    vals = [1.0 if p.winner == "a" else (0.5 if p.winner == "tie" else 0.0) for p in pairs]
    ci = _bootstrap_mean_ci(vals, n=cfg.n_bootstrap) if cfg.bootstrap_ci else None

    low = (agree is not None and agree < threshold) or not bidi
    rationale = " | ".join(f"[{p.judge}/{p.order}] {p.rationale}" for p in pairs[:3])

    return PairResult(
        item_id=pairs[0].item_id,
        model_a=pairs[0].model_a,
        model_b=pairs[0].model_b,
        a_wins=a,
        b_wins=b,
        ties=t,
        winner=winner,
        win_rate_a=win_rate_a,
        rationale=rationale,
        agreement=agree,
        bidirectional_consistent=bidi,
        low_agreement=low,
        single_pairs=pairs,
    )
