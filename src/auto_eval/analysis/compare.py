"""待评测 vs 竞品对比：胜率、分维度差、按类目胜率。"""
from __future__ import annotations

import collections

import numpy as np

from ..schema import EvalItem, PairResult, Verdict


def pairwise_winrate(pairs, focal: str, other: str) -> dict:
    rel = [p for (_iid, ma, mb), p in pairs.items() if ma == focal and mb == other]
    if not rel:
        return {"n_items": 0, "focal_winrate": None}
    a = sum(p.a_wins for p in rel)
    b = sum(p.b_wins for p in rel)
    t = sum(p.ties for p in rel)
    n = a + b + t
    return {
        "n_items": len(rel),
        "n_comparisons": n,
        "focal_wins": a,
        "other_wins": b,
        "ties": t,
        "focal_winrate": (a + 0.5 * t) / n if n else 0.0,
        "low_agreement_items": sum(1 for p in rel if p.low_agreement),
        "bidirectional_inconsistent": sum(1 for p in rel if not p.bidirectional_consistent),
    }


def pairwise_by_category(pairs, items_map, focal: str, other: str) -> dict:
    groups: dict[str, list[PairResult]] = collections.defaultdict(list)
    for (_iid, ma, mb), p in pairs.items():
        if ma != focal or mb != other:
            continue
        item = items_map.get(p.item_id)
        if not item:
            continue
        for c in item.categories():
            groups[c].append(p)
    out: dict[str, float] = {}
    for c, ps in groups.items():
        a = sum(p.a_wins for p in ps)
        b = sum(p.b_wins for p in ps)
        t = sum(p.ties for p in ps)
        n = a + b + t
        out[c] = (a + 0.5 * t) / n if n else 0.0
    return out


def dim_gap(verdicts, focal: str, other: str) -> dict:
    """focal 与 other 在共同题上各维度均分之差（focal − other）。"""
    fv = {v.item_id: v for v in verdicts if v.model == focal}
    ov = {v.item_id: v for v in verdicts if v.model == other}
    common = set(fv) & set(ov)
    dim_names: set[str] = set()
    for iid in common:
        dim_names.update(fv[iid].rubric.keys())
        dim_names.update(ov[iid].rubric.keys())
    # 排除双方共识的 N/A 维度
    na_skip: set[str] = set()
    for iid in common:
        na_skip.update(fv[iid].na_dimensions)
        na_skip.update(ov[iid].na_dimensions)
    active_dims = dim_names - na_skip
    gap: dict[str, float] = {}
    for d in active_dims:
        fs = float(np.mean([fv[i].rubric.get(d, 0) for i in common])) if common else 0.0
        os_ = float(np.mean([ov[i].rubric.get(d, 0) for i in common])) if common else 0.0
        gap[d] = fs - os_
    return {"n_common": len(common), "dim_gap": gap}
