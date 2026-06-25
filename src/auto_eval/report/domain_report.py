"""分垂域报表 C：以垂域（Skill）为主轴组织被测模型表现 + 维度问题分布 + 错因聚类 + 可靠性。

复用 aggregate/cluster/per_case，换组织主轴：每条题经 SkillRouter.resolve 归一到 Skill，
按 Skill 分组聚合。垂域名用 Skill 的 display（中文）。
"""
from __future__ import annotations

import collections

from ..analysis.aggregate import model_overview
from ..analysis.cluster import cluster_weaknesses
from ..analysis.per_case import category_source
from ..config import AppConfig
from ..engine import EvalResults
from ..judges.skill_router import SkillRouter
from ..schema import EvalItem, ModelOutput


def _f(x, d=2) -> str:
    return f"{x:.{d}f}" if isinstance(x, (int, float)) else "—"


def _pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "—"


def _skill_of(skill_router: SkillRouter | None, items: list[EvalItem]) -> dict[str, str]:
    if not skill_router:
        return {it.id: "default" for it in items}
    return {it.id: skill_router.resolve(it) for it in items}


def _ordered_skills(skill_router: SkillRouter | None) -> list[str]:
    """非 default 在前（按加载序），default 垫底。"""
    if not skill_router:
        return ["default"]
    names = [n for n in skill_router.domain.keys() if n != "default"]
    return names + (["default"] if "default" in skill_router.domain else [])


def _pair_winrate_in(pairs, focal, other, skill_of, skill_name) -> float | None:
    a = b = t = 0
    for (_iid, ma, mb), p in pairs.items():
        if ma != focal or mb != other:
            continue
        if skill_of.get(p.item_id) != skill_name:
            continue
        a += p.a_wins
        b += p.b_wins
        t += p.ties
    n = a + b + t
    return (a + 0.5 * t) / n if n else None


def _meta_acc_in(metas, ids_in_skill) -> float | None:
    rel = [m for m in metas if m.item_id in ids_in_skill and m.has_ref and m.agree is not None]
    if not rel:
        return None
    return sum(1 for m in rel if m.agree) / len(rel)


def _dim_problem_dist(focal_verdicts, threshold, n_items, scale) -> dict[str, dict]:
    """各一级维度：分 <= threshold 的题占比 + 题号（维度问题分布）。

    缺失维度按满分（scale）处理，即不算问题。返回 {dim: {rate, item_ids}}。
    """
    all_dims: set[str] = set()
    for v in focal_verdicts:
        all_dims.update(v.rubric.keys())
    out: dict[str, dict] = {}
    for dim in sorted(all_dims):
        ids = [v.item_id for v in focal_verdicts if v.rubric.get(dim, scale) <= threshold]
        out[dim] = {
            "rate": (len(ids) / n_items) if n_items else 0.0,
            "count": len(ids),
            "item_ids": ids,
        }
    return out


def _summarize(focal_ov, clusters, low_rate, scale) -> str:
    parts: list[str] = []
    dims = focal_ov.get("dim_means", {})
    if dims:
        weak = sorted(dims.items(), key=lambda x: x[1])[0]
        flag = " ⚠️偏低" if weak[1] < 0.6 * scale else ""
        parts.append(f"短板维度：{weak[0]}（{_f(weak[1])}/{scale}{flag}）")
    if clusters:
        parts.append(f"首要错因簇：{clusters[0]['label']}（{clusters[0]['count']} 题）")
    if low_rate and low_rate > 0.2:
        parts.append(f"⚠️低一致率占比 {_pct(low_rate)}，结论建议人工复核")
    return "；".join(parts) if parts else "本垂域样本表现均衡，无明显短板。"


def _build_overview(sections, focal) -> list[dict]:
    rows = []
    for s in sections:
        fov = s["per_model"].get(focal, {})
        right = fov.get("correctness_dist", {}).get("right", 0)
        n = fov.get("n", 0)
        rows.append({
            "display": s["display"],
            "skill": s["skill"],
            "n_items": s["n_items"],
            "focal_right_rate": (right / n) if n else None,
            "focal_mean_total": fov.get("mean_total"),
            "first_cluster": s["clusters"][0]["label"] if s["clusters"] else "—",
            "low_agreement_rate": s["low_agreement_rate"],
            "fallback_pct": s["category_source_pct"].get("fallback_default"),
        })
    return rows


def _build_section(skill_name, skill_router, results, items_map, skill_of,
                   focal, models, others, scale, clusters_by_skill, threshold) -> dict:
    ids_in = {iid for iid, sk in skill_of.items() if sk == skill_name}
    items_in = [items_map[i] for i in ids_in if i in items_map]
    all_verdicts = list(results.verdicts.values())
    focal_verdicts_in = [v for v in all_verdicts if v.model == focal and skill_of.get(v.item_id) == skill_name]

    per_model = {
        m: model_overview(
            [v for v in all_verdicts if v.model == m and skill_of.get(v.item_id) == skill_name], scale
        )
        for m in models
    }
    focal_ov = per_model.get(focal, {})
    n_items = len(items_in)
    src_cnt = collections.Counter(category_source(it) for it in items_in)
    src_pct = {k: v / n_items for k, v in src_cnt.items()} if n_items else {}

    return {
        "skill": skill_name,
        "display": (skill_router.display_of(skill_name) if skill_router else "通用"),
        "n_items": n_items,
        "category_source_pct": src_pct,
        "per_model": per_model,
        "winrate_vs": {o: _pair_winrate_in(results.pairs, focal, o, skill_of, skill_name) for o in others},
        "clusters": clusters_by_skill.get(skill_name, []),
        "dim_problem_dist": _dim_problem_dist(focal_verdicts_in, threshold, n_items, scale),
        "low_agreement_rate": focal_ov.get("low_agreement_rate", 0.0),
        "meta_accuracy": _meta_acc_in(results.metas, ids_in),
        "summary": _summarize(focal_ov, clusters_by_skill.get(skill_name, []),
                              focal_ov.get("low_agreement_rate", 0.0), scale),
    }


def build_domain_report(
    results: EvalResults,
    items: list[EvalItem],
    ans_index: dict[str, dict[str, ModelOutput]],
    cfg: AppConfig,
    skill_router: SkillRouter | None,
    run_id: str,
) -> dict:
    """以垂域（Skill）为主轴的报表。返回 {"C": {...}, "C_md": "..."}。"""
    items_map = {it.id: it for it in items}
    # models 从实际 verdicts 推断（CLI=cfg 的模型；web 统一为 "answer"），兜底用配置
    models = sorted({v.model for v in results.verdicts.values()}) or cfg.model_names()
    focal = results.focal_model or (models[0] if models else cfg.model_names()[0])
    others = [m for m in models if m != focal]
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    threshold = getattr(cfg.ensemble, "dim_problem_threshold", 2.0)
    skill_of = _skill_of(skill_router, items)

    clusters_by_skill = cluster_weaknesses(results.verdicts, items_map, skill_of, focal)
    sections = [
        _build_section(sk, skill_router, results, items_map, skill_of,
                       focal, models, others, scale, clusters_by_skill, threshold)
        for sk in _ordered_skills(skill_router)
    ]
    C = {
        "run_id": run_id,
        "focal": focal,
        "scale": scale,
        "dim_problem_threshold": threshold,
        "overview": _build_overview(sections, focal),
        "sections": sections,
    }
    return {"C": C, "C_md": _render_c_md(C, focal)}


def _render_c_md(C, focal) -> str:
    L = [f"# 分垂域数据报表 · {C['run_id']}", ""]
    L.append(f"> 主轴：垂域（Skill）。focal = **{focal}**；每条题经 SkillRouter 归一到垂域。")
    L.append("")

    L.append("## 一、总览（一行一垂域，扫一眼定位最弱垂域）")
    L.append("| 垂域 | 样本量 | focal正确率 | focal平均分 | 首要错因 | 低一致率 | 分类失败占比 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in C["overview"]:
        L.append(
            f"| {r['display']} | {r['n_items']} | "
            f"{_pct(r['focal_right_rate'])} | {_f(r['focal_mean_total'])} | "
            f"{r['first_cluster']} | {_pct(r['low_agreement_rate'])} | {_pct(r['fallback_pct'])} |"
        )
    L.append("")

    L.append("## 二、各垂域详情")
    thr = C.get("dim_problem_threshold", 2.0)
    for s in C["sections"]:
        L.append(f"### {s['display']}（{s['skill']}，N={s['n_items']}）")
        if not s["n_items"]:
            L.append("- 本垂域无样本。")
            L.append("")
            continue
        src = s["category_source_pct"]
        L.append(
            f"- 分类来源：dataset {_pct(src.get('dataset'))} / 自动分类 {_pct(src.get('auto_classified'))} "
            f"/ 兜底 {_pct(src.get('fallback_default'))}"
        )
        L.append("| 模型 | 题数 | 平均分 | right占比 | 低一致率 |")
        L.append("|---|---|---|---|---|")
        for m, ov in s["per_model"].items():
            if ov.get("n", 0) == 0:
                L.append(f"| {m} | 0 | — | — | — |")
                continue
            right = ov["correctness_dist"].get("right", 0)
            L.append(f"| {m} | {ov['n']} | {_f(ov['mean_total'])} | {_pct(right / ov['n'])} | {_pct(ov['low_agreement_rate'])} |")
        dims = s["per_model"].get(focal, {}).get("dim_means", {})
        if dims:
            L.append(f"- focal 维度均分（升序）：{', '.join(f'{k} {_f(v)}' for k, v in sorted(dims.items(), key=lambda x: x[1]))}")
        wr = "、".join(f"vs {o} {_pct(w)}" for o, w in s["winrate_vs"].items() if w is not None)
        if wr:
            L.append(f"- 此垂域成对胜率：{wr}")
        # 维度问题分布（一级，带题号）
        dpd = s.get("dim_problem_dist") or {}
        if dpd:
            L.append(f"- 维度问题分布（维度分 ≤ {thr} 视为问题，含题号）：")
            for dim, info in sorted(dpd.items(), key=lambda kv: -kv[1]["rate"]):
                ids = ", ".join(info["item_ids"][:10])
                L.append(f"  - {dim}：{_pct(info['rate'])}[{ids}]")
        if s["clusters"]:
            L.append("- 错因聚类：")
            for c in s["clusters"][:5]:
                kws = "/".join(c["keywords"]) if c["keywords"] else ""
                L.append(f"  - 【{c['label']}】{c['count']} 题{f' 关键词:{kws}' if kws else ''} —— {c['represent']}")
        else:
            L.append("- 错因聚类：无错题（focal 本垂域全对）。")
        rel_bits = [f"低一致率 {_pct(s['low_agreement_rate'])}"]
        if s["meta_accuracy"] is not None:
            rel_bits.append(f"元评测判题准确率 {_pct(s['meta_accuracy'])}")
        L.append(f"- 可靠性：{'，'.join(rel_bits)}")
        L.append(f"- **小结**：{s['summary']}")
        L.append("")
    return "\n".join(L)
