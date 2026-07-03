"""错因聚类：按垂域（Skill）分组，垂域内按归一化 error_type 聚类 + 双字关键词。

轻量线（零依赖，无 sklearn/embedding）：error_type 关键词归一 + 中文双字 2-gram 关键词。
语义线（embedding + KMeans/HDBSCAN）留作后续升级，接口保持 cluster_weaknesses 不变。
"""
from __future__ import annotations

import collections
import re

from ..schema import EvalItem, Verdict


# error_type 关键词 → 归一标签（裁判自由给出的 error_type 往这些桶里套）
_ERROR_RULES = [
    ("事实", "事实错误"),
    ("计算", "计算错误"),
    ("逻辑", "逻辑缺失"),
    ("不完整", "不完整"),
    ("遗漏", "不完整"),
    ("指令", "指令偏离"),
    ("跑题", "指令偏离"),
    ("偏题", "指令偏离"),
    ("风险", "含风险内容"),
    ("安全", "含风险内容"),
    ("有害", "含风险内容"),
]

# 双字关键词的停用字（高频无义单字）
_STOP = set("的了是我你他她它们这那有就和也都还啊吧呢吗呀把被让给跟与在到上下里外中以及为会能可要对一不是")


def normalize_error_type(et: str | None) -> str:
    """把裁判自由给出的 error_type 归一到标准桶；无法归一则原样保留；空 → 未归类。"""
    if not et:
        return "未归类"
    for key, label in _ERROR_RULES:
        if key in et:
            return label
    return et


def _bigrams(text: str, topn: int = 4) -> list[str]:
    """中文双字 2-gram 频次关键词（去标点/停用字），零依赖的中文关键词近似。"""
    text = re.sub(r"[\s\W_]+", "", text or "")
    cnt: collections.Counter = collections.Counter()
    for i in range(len(text) - 1):
        bg = text[i : i + 2]
        if bg[0] in _STOP or bg[1] in _STOP:
            continue
        cnt[bg] += 1
    return [bg for bg, _ in cnt.most_common(topn)]


def cluster_weaknesses(
    verdicts,
    items_map: dict[str, EvalItem],
    skill_of: dict[str, str],
    focal: str,
) -> dict[str, list[dict]]:
    """focal 的错题(wrong/partial) 按垂域分组、垂域内按归一 error_type 聚类。

    verdicts: dict[(iid,model),Verdict] 或可迭代的 Verdict。
    skill_of: iid → skill name（由 SkillRouter.resolve 预算）。
    返回 {skill_name: [{label, count, item_ids, keywords, represent}]}，垂域内按 count 降序。
    """
    vs = verdicts.values() if isinstance(verdicts, dict) else verdicts
    bad = [v for v in vs if v.model == focal and v.correctness in ("wrong", "partial")]

    buckets: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for v in bad:
        sk = skill_of.get(v.item_id, "default")
        buckets[(sk, normalize_error_type(v.error_type))].append(v.item_id)

    out: dict[str, list[dict]] = collections.defaultdict(list)
    for (sk, label), ids in buckets.items():
        questions = [
            " ".join(
                x for x in (
                    items_map[i].question,
                    items_map[i].context or "",
                ) if x
            ) if items_map.get(i) else ""
            for i in ids
        ]
        kws: list[str] = []
        for q in questions:
            kws += _bigrams(q)
        kw_top = [k for k, _ in collections.Counter(kws).most_common(5)]
        out[sk].append({
            "label": label,
            "count": len(ids),
            "item_ids": ids[:10],
            "keywords": kw_top,
            "represent": (questions[0][:120] if questions else ""),
        })
    for sk in out:
        out[sk].sort(key=lambda c: -c["count"])
    return dict(out)
