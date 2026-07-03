"""典型 case 挖掘：focal 赢最多/输最多、多裁判分歧最大。"""
from __future__ import annotations

import numpy as np

from ..schema import ModelOutput


def _ans_text(ans_index, model, iid, limit=300) -> str:
    out = ans_index.get(model, {}).get(iid)
    return (out.answer[:limit] if out and out.answer else "")


def focal_vs_competitors(
    pairs, items_map, ans_index, focal: str, others: list[str], n: int = 5
) -> dict:
    """focal 对各竞品的逐题胜率 → 赢最多/输最多的题。"""
    per_item: dict[str, dict[str, float]] = {}
    for other in others:
        for (_iid, ma, mb), p in pairs.items():
            if ma != focal or mb != other:
                continue
            tot = p.a_wins + p.b_wins + p.ties
            wr = (p.a_wins + 0.5 * p.ties) / tot if tot else 0.5
            per_item.setdefault(p.item_id, {})[other] = wr

    avg = {iid: float(np.mean(list(d.values()))) for iid, d in per_item.items() if d}
    ordered = sorted(avg.items(), key=lambda x: x[1])
    worst = ordered[:n]
    best = list(reversed(ordered[-n:])) if ordered else []

    def build(iid: str) -> dict:
        item = items_map.get(iid)
        return {
            "item_id": iid,
            "question": (item.question[:200] if item else ""),
            "context": (item.context[:500] if item and item.context else ""),
            "category": (item.categories() if item else []),
            "difficulty": (item.difficulty if item else ""),
            "focal_answer": _ans_text(ans_index, focal, iid),
            "per_competitor_winrate": per_item.get(iid, {}),
        }

    return {
        "n": len(avg),
        "worst": [build(i) for i, _ in worst],
        "best": [build(i) for i, _ in best],
    }


def disagreement_cases(verdicts, n: int = 5) -> list[dict]:
    """多裁判一致率最低的题（评测盲区信号）。"""
    vs = [v for v in verdicts.values() if v.judges_agreement is not None]
    vs.sort(key=lambda v: (v.judges_agreement if v.judges_agreement is not None else 1.0))
    return [
        {
            "item_id": v.item_id,
            "model": v.model,
            "agreement": v.judges_agreement,
            "correctness": v.correctness,
            "total": v.total,
        }
        for v in vs[:n]
    ]
