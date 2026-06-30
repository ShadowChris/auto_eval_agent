"""被测模型指标聚合与切片。"""
from __future__ import annotations

import collections
from typing import Callable

import numpy as np

from ..schema import EvalItem, Verdict


def model_overview(verdicts: list[Verdict], scale: int) -> dict:
    if not verdicts:
        return {"n": 0}
    totals = [v.total for v in verdicts]
    dim_names: set[str] = set()
    for v in verdicts:
        dim_names.update(v.rubric.keys())
    # 排除多裁判共识的 N/A 维度（该维度对本题集不适用，不计入均分统计）
    na_skip: set[str] = set()
    for v in verdicts:
        na_skip.update(v.na_dimensions)
    active_dims = dim_names - na_skip
    dim_means = {d: float(np.mean([v.rubric.get(d, 0) for v in verdicts])) for d in active_dims}
    return {
        "n": len(verdicts),
        "mean_total": float(np.mean(totals)),
        "norm_total": float(np.mean(totals)) / scale if scale else 0.0,
        "dim_means": dim_means,
        "correctness_dist": dict(collections.Counter(v.correctness for v in verdicts)),
        "low_agreement_rate": sum(1 for v in verdicts if v.low_agreement) / len(verdicts),
    }


def overview_by_slice(
    verdicts: list[Verdict],
    items_map: dict[str, EvalItem],
    scale: int,
    slice_key: str = "category",
) -> dict:
    groups: dict[str, list[Verdict]] = collections.defaultdict(list)
    for v in verdicts:
        item = items_map.get(v.item_id)
        if not item:
            continue
        if slice_key == "category":
            keys = item.categories()
        elif slice_key == "difficulty":
            keys = [item.difficulty]
        elif slice_key == "has_ref":
            keys = ["有参考答案" if item.has_ref else "无参考答案"]
        else:
            keys = ["all"]
        for k in keys:
            groups[k].append(v)
    return {k: model_overview(vs, scale) for k, vs in groups.items()}


def per_model(verdicts, items_map, models, scale) -> dict:
    """每个模型的总体 overview。"""
    return {
        m: model_overview([v for v in verdicts if v.model == m], scale)
        for m in models
    }
