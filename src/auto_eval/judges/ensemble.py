"""多裁判聚合：rubric 去极值均值、分类多数投票、一致率、重复稳定性、双向一致性、Bootstrap CI。"""
from __future__ import annotations

import collections
from typing import Optional

import numpy as np

from ..config import EnsembleConfig, RubricDim
from ..schema import PairResult, SinglePair, SingleScore, Verdict

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
    ets = [s.error_type for s in scores if s.error_type]
    error_type = _majority(ets) if ets else None

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

    return Verdict(
        item_id=scores[0].item_id,
        model=scores[0].model,
        rubric=rubric_mean,
        rubric_reasons=rubric_reasons,
        na_dimensions=na_consensus,
        total=total,
        correctness=correctness,
        error_type=error_type,
        rationale=" | ".join(f"[{s.judge}] {s.rationale}" for s in scores[:3]),
        n_judges=len(by_judge),
        judges_agreement=agree,
        repeat_std=repeat_std,
        low_agreement=low,
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
