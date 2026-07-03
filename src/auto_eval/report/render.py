"""报告生成：A 被测模型对比报告 + B 评测 agent 可靠性报告（各 md + json）。"""
from __future__ import annotations

from typing import Any

from ..analysis import (
    advise,
    build_case_rows,
    cases_to_csv,
    dim_gap,
    disagreement_cases,
    focal_vs_competitors,
    overview_by_slice,
    pairwise_by_category,
    pairwise_winrate,
    per_model,
    weaknesses,
)
from ..config import AppConfig
from ..engine import EvalResults
from ..judges import SkillRouter
from ..meta import summarize, summarize_noref
from ..schema import EvalItem, ModelOutput

from .domain_report import build_domain_report


def _f(x, d=2) -> str:
    return f"{x:.{d}f}" if isinstance(x, (int, float)) else "—"


def _pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "—"


def build_reports(
    results: EvalResults,
    items: list[EvalItem],
    ans_index: dict[str, dict[str, ModelOutput]],
    cfg: AppConfig,
    run_id: str,
) -> dict[str, Any]:
    focal = results.focal_model or cfg.model_names()[0]
    models = cfg.model_names()
    others = [m for m in models if m != focal]
    items_map = {it.id: it for it in items}
    scale = cfg.rubrics[0].scale if cfg.rubrics else 5
    all_verdicts = list(results.verdicts.values())

    per_ov = per_model(all_verdicts, items_map, models, scale)
    winrate_vs = {o: pairwise_winrate(results.pairs, focal, o) for o in others}
    winrate_cat = {o: pairwise_by_category(results.pairs, items_map, focal, o) for o in others}
    dim_gaps = {o: dim_gap(all_verdicts, focal, o) for o in others}
    cases = focal_vs_competitors(results.pairs, items_map, ans_index, focal, others)
    disagreements = disagreement_cases(results.verdicts)
    weakness_list = weaknesses(results.verdicts, items_map, focal)
    advice_md = advise(focal, per_ov.get(focal, {}), weakness_list, winrate_vs, scale)
    focal_cat = overview_by_slice(
        [v for v in all_verdicts if v.model == focal], items_map, scale, "category"
    )

    A = {
        "run_id": run_id,
        "focal": focal,
        "competitors": others,
        "scale": scale,
        "per_model": per_ov,
        "winrate_vs": winrate_vs,
        "winrate_by_category": winrate_cat,
        "dim_gap_vs": dim_gaps,
        "focal_by_category": focal_cat,
        "cases": cases,
        "disagreement_cases": disagreements,
        "weaknesses": weakness_list,
        "advice_md": advice_md,
    }

    # 给典型 case 附上裁判查证轨迹（体现 agent loop 的联网/工具调用）
    for c in A["cases"]["worst"] + A["cases"]["best"]:
        v = results.verdicts.get((c["item_id"], focal))
        if v and v.single_scores:
            c["focal_tool_trace"] = v.single_scores[0].tool_trace
        else:
            c["focal_tool_trace"] = []
    # 挑选有工具调用的 verdict 作为查证轨迹示例
    trace_examples = []
    for (iid, model), v in results.verdicts.items():
        if v.single_scores and v.single_scores[0].tool_trace:
            it = items_map.get(iid)
            trace_examples.append({
                "item_id": iid,
                "model": model,
                "tool_trace": v.single_scores[0].tool_trace,
                "correctness": v.correctness,
                "category": (it.categories() if it else []),
            })
        if len(trace_examples) >= 8:
            break
    A["judge_trace_examples"] = trace_examples

    meta_summary = summarize(results.metas)
    noref_summary = summarize_noref(results.verdicts, items_map)
    B = {
        "run_id": run_id,
        "judges": cfg.judge_names(),
        "rubric_dims": [d.name for d in cfg.rubrics],
        "eval_options": cfg.eval_options.model_dump(),
        "meta_summary": meta_summary,
        "noref_summary": noref_summary,
    }

    skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
    dom = build_domain_report(results, items, ans_index, cfg, skill_router, run_id)
    case_rows = build_case_rows(results, items, ans_index, cfg, skill_router)
    cases_csv = cases_to_csv(case_rows, cfg.model_names(), focal)
    return {
        "A": A, "A_md": _render_a_md(A),
        "B": B, "B_md": _render_b_md(B),
        "C": dom["C"], "C_md": dom["C_md"],
        "cases_csv": cases_csv,
    }


# --------------------------------------------------------------------------- #
# A：被测模型对比报告
# --------------------------------------------------------------------------- #
def _render_a_md(A: dict) -> str:
    scale = A["scale"]
    focal = A["focal"]
    L: list[str] = [f"# 被测模型对比报告 · {A['run_id']}", ""]

    L.append("## 一、各模型总体得分")
    L.append(f"| 模型 | 题数 | 平均总分(满分{scale}) | 归一化 | right占比 | 低一致率 |")
    L.append("|---|---|---|---|---|---|")
    for m, ov in A["per_model"].items():
        if ov.get("n", 0) == 0:
            L.append(f"| {m} | 0 | — | — | — | — |")
            continue
        dist = ov["correctness_dist"]
        right = dist.get("right", 0)
        L.append(
            f"| {m} | {ov['n']} | {_f(ov['mean_total'])} | {_pct(ov['norm_total'])} | "
            f"{_pct(right/ov['n'])} | {_pct(ov['low_agreement_rate'])} |"
        )
    L.append("")

    L.append("## 二、待评测 vs 竞品 成对胜率（Win Rate）")
    if A["winrate_vs"]:
        L.append("| 竞品 | focal 胜率 | focal胜 / 竞品胜 / 平 | 双向不一致题数 | 低一致率题数 |")
        L.append("|---|---|---|---|---|")
        for other, wr in A["winrate_vs"].items():
            if wr.get("n_items", 0) == 0:
                L.append(f"| {other} | — | 未生成成对比较 | — | — |")
            else:
                L.append(
                    f"| {other} | **{_pct(wr['focal_winrate'])}** | "
                    f"{wr['focal_wins']} / {wr['other_wins']} / {wr['ties']} | "
                    f"{wr['bidirectional_inconsistent']} | {wr['low_agreement_items']} |"
                )
    else:
        L.append("- 无竞品或未生成成对比较（若有参考答案题较多，可在 judges.yaml 开启 `pairwise_for_ref`）。")
    L.append("")

    L.append("## 三、按类目胜率（focal vs 竞品）")
    cats = sorted({c for d in A["winrate_by_category"].values() for c in d})
    if cats and A["winrate_vs"]:
        head = ["类目"] + [o for o in A["winrate_vs"]]
        L.append("| " + " | ".join(head) + " |")
        L.append("|" + "|".join(["---"] * len(head)) + "|")
        for c in cats:
            row = [c] + [_pct(A["winrate_by_category"][o].get(c)) for o in A["winrate_vs"]]
            L.append("| " + " | ".join(row) + " |")
    else:
        L.append("- 无类目胜率数据。")
    L.append("")

    L.append("## 四、各维度均分差（focal − 竞品；负值=focal 落后）")
    if A["dim_gap_vs"]:
        for other, dg in A["dim_gap_vs"].items():
            L.append(f"### vs {other}（共同题 {dg['n_common']}）")
            if dg["dim_gap"]:
                L.append("| 维度 | 差值 |")
                L.append("|---|---|")
                for d, g in sorted(dg["dim_gap"].items(), key=lambda x: x[1]):
                    L.append(f"| {d} | {_f(g)} |")
            L.append("")
    L.append("")

    L.append("## 五、典型 case")
    L.append("### focal 输最多的题")
    for c in A["cases"]["worst"]:
        wr = c["per_competitor_winrate"]
        avg = sum(wr.values()) / len(wr) if wr else 0
        L.append(f"- **{c['item_id']}** [{'/'.join(c['category'])}] 平均胜率 {_pct(avg)}")
        L.append(f"  - 题：{c['question']}")
        if c.get("context"):
            L.append(f"  - 背景：{c['context']}")
        L.append(f"  - focal 答：{c['focal_answer']}")
        if c.get("focal_tool_trace"):
            L.append(f"  - 裁判查证轨迹：{' | '.join(c['focal_tool_trace'])}")
    L.append("")
    L.append("### focal 赢最多的题")
    for c in A["cases"]["best"]:
        wr = c["per_competitor_winrate"]
        avg = sum(wr.values()) / len(wr) if wr else 0
        L.append(f"- **{c['item_id']}** [{'/'.join(c['category'])}] 平均胜率 {_pct(avg)}：{c['question']}")
        if c.get("context"):
            L.append(f"  - 背景：{c['context']}")
    L.append("")

    L.append("## 裁判查证轨迹示例（agent loop 的联网/工具调用）")
    if A.get("judge_trace_examples"):
        for ex in A["judge_trace_examples"]:
            L.append(
                f"- **{ex['item_id']}** / {ex['model']} [{'/'.join(ex['category'])}] → 判定 {ex['correctness']}"
            )
            for t in ex["tool_trace"]:
                L.append(f"  - {t}")
    else:
        L.append("- 本批裁判未触发工具调用（题目无需联网/计算核查）。")
    L.append("")

    L.append("## 六、多裁判分歧最大的题（评测盲区信号）")
    for c in A["disagreement_cases"]:
        L.append(f"- {c['item_id']} / {c['model']}：一致率 {_f(c['agreement'])}，判定 {c['correctness']}")
    L.append("")

    L.append("## 七、优化建议")
    L.append(A["advice_md"])
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# B：评测 agent 可靠性报告
# --------------------------------------------------------------------------- #
def _render_b_md(B: dict) -> str:
    ms = B["meta_summary"]
    nr = B["noref_summary"]
    L: list[str] = [f"# 评测 Agent 可靠性报告 · {B['run_id']}", ""]

    L.append("> 本报告衡量的是**评测 agent 本身**是否可信，而非被测模型。")
    L.append("> 用参考答案作为 ground truth，对照盲评结论。")
    L.append("")

    L.append("## 一、判题准确性（有参考答案题）")
    L.append(f"- 有参考答案题数：**{ms['n_has_ref']}**")
    L.append(f"- 评测 agent 判题准确率：**{_pct(ms['judge_accuracy'])}**")
    L.append(f"- 偏差方向：**{ms['bias_direction']}**（bias = {_f(ms['bias_judge_vs_sim'])}）")
    L.append(f"- 盲评分与客观相似度相关性(Pearson)：**{_f(ms['score_corr_judge_vs_sim'], 3)}**")
    L.append(f"- 与真值不一致题数：{ms['n_disagreements']}")
    L.append("")

    L.append("## 二、各类目判题准确率")
    if ms["category_accuracy"]:
        L.append("| 类目 | 准确率 |")
        L.append("|---|---|")
        for c, a in sorted(ms["category_accuracy"].items(), key=lambda x: x[1]):
            L.append(f"| {c} | {_pct(a)} |")
    L.append("")

    L.append("## 三、各难度判题准确率")
    if ms["difficulty_accuracy"]:
        L.append("| 难度 | 准确率 |")
        L.append("|---|---|")
        for d, a in ms["difficulty_accuracy"].items():
            L.append(f"| {d} | {_pct(a)} |")
    L.append("")

    L.append("## 四、判错 case 清单（盲评与客观真值冲突）")
    for d in ms["disagreements"][:30]:
        L.append(
            f"- {d['item_id']} / {d['model']} [{d['category']}]："
            f"客观={d['objective_correct']}，盲评={d['judge_correctness']}"
        )
    if len(ms["disagreements"]) > 30:
        L.append(f"- ...共 {len(ms['disagreements'])} 条，详见 JSON")
    L.append("")

    L.append("## 五、无参考答案题的盲评可信度")
    if nr.get("n_no_ref", 0) == 0:
        L.append("- 无无参考答案题。")
    else:
        L.append(f"- 无参考答案题数：**{nr['n_no_ref']}**")
        L.append(f"- 多裁判平均一致率：**{_f(nr['mean_judges_agreement'], 3)}**")
        L.append(f"- 低一致率(需人工复核)占比：**{_pct(nr['low_agreement_rate'])}**")
        L.append(f"- 建议复核题数：{nr['flagged_for_review']}")
    L.append("")

    L.append("## 六、可信度结论")
    acc = ms["judge_accuracy"]
    if acc is None:
        concl = "无有参考答案题，无法给出判题准确率；请参考无参考答案题的多裁判一致率。"
    elif acc >= 0.85:
        concl = "评测 agent 整体可信（判题准确率高），结论可直接用于决策。"
    elif acc >= 0.7:
        concl = "评测 agent 基本可信，但存在系统性偏差或盲区，建议结合人工抽检。"
    else:
        concl = "评测 agent 可信度偏低，建议调整裁判模型/rubric/联网检索后重测，结论仅供参考。"
    L.append(f"- {concl}")
    L.append("")
    L.append(f"裁判配置：{', '.join(B['judges'])}")
    L.append(f"评测维度：{', '.join(B['rubric_dims'])}")
    return "\n".join(L)
