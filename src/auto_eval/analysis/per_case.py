"""逐条诊断卡：把一道题上散落的盲评/成对/元评测信号汇成一行，可导出 CSV。

纯规则拼接，不调 LLM；标红 case 的自然语言小结可在更上层选择性增强。
category_source 来自 rubric_judge 打的标记（dataset/auto_classified/fallback_default），
缺失时按 category 字段推断。
"""
from __future__ import annotations

import csv
import io

from ..config import AppConfig
from ..engine import EvalResults
from ..judges.skill_router import SkillRouter
from ..schema import EvalItem, ModelOutput


def _ans_text(ans_index, model, iid, limit=300) -> str:
    out = ans_index.get(model, {}).get(iid)
    return (out.answer[:limit] if out and out.answer else "")


def category_source(item: EvalItem) -> str:
    """分类来源：优先读 metadata 标记；缺失则按 category 推断。"""
    src = item.metadata.get("category_source")
    if src:
        return src
    return "fallback_default" if item.category in (None, "", "default") else "dataset"


def _flags(focal_v, pair_winrates: dict, meta_info) -> list[str]:
    """诊断标签：低一致率 / focal 判错 / 元评测判错 / focal 此题落后竞品。"""
    flags: list[str] = []
    if focal_v and focal_v.get("low_agreement"):
        flags.append("低一致率")
    if focal_v and focal_v.get("correctness") in ("wrong", "partial"):
        flags.append(f"focal判{focal_v.get('correctness')}")
    if meta_info and meta_info.get("agree") is False:
        flags.append("元评测判错")
    if any((wr is not None and wr < 0.5) for wr in pair_winrates.values()):
        flags.append("focal此题落后")
    return flags


def build_case_rows(
    results: EvalResults,
    items: list[EvalItem],
    ans_index: dict[str, dict[str, ModelOutput]],
    cfg: AppConfig,
    skill_router: SkillRouter | None = None,
) -> list[dict]:
    """每条题一行诊断卡（dict）。"""
    focal = results.focal_model or cfg.model_names()[0]
    models = cfg.model_names()
    others = [m for m in models if m != focal]
    rows: list[dict] = []
    for item in items:
        iid = item.id
        skill = skill_router.resolve(item) if skill_router else "default"
        skill_display = skill_router.display_of(skill) if skill_router else "通用"

        per_model: dict[str, dict] = {}
        for m in models:
            v = results.verdicts.get((iid, m))
            if v:
                per_model[m] = {
                    "correctness": v.correctness,
                    "total": v.total,
                    "rubric": v.rubric,
                    "error_type": v.error_type,
                    "judges_agreement": v.judges_agreement,
                    "low_agreement": v.low_agreement,
                    "arbitrated": v.arbitrated,
                }

        pair_wr: dict[str, float | None] = {}
        for o in others:
            p = results.pairs.get((iid, focal, o))
            if p:
                tot = p.a_wins + p.b_wins + p.ties
                pair_wr[o] = (p.a_wins + 0.5 * p.ties) / tot if tot else None

        meta = next((mm for mm in results.metas if mm.item_id == iid and mm.model == focal), None)
        meta_info = None
        if meta and meta.has_ref:
            meta_info = {
                "objective_correct": meta.objective_correct,
                "judge_correctness": meta.judge_correctness,
                "agree": meta.agree,
            }

        rows.append({
            "item_id": iid,
            "skill": skill,
            "skill_display": skill_display,
            "category": item.categories(),
            "category_source": category_source(item),
            "difficulty": item.difficulty,
            "has_ref": item.has_ref,
            "question": (item.question[:200] or ""),
            "per_model": per_model,
            "pair_winrate_vs": pair_wr,
            "meta": meta_info,
            "flags": _flags(per_model.get(focal), pair_wr, meta_info),
            "focal_answer": _ans_text(ans_index, focal, iid),
        })
    return rows


def cases_to_csv(rows: list[dict], models: list[str], focal: str) -> str:
    """诊断卡扁平化成 CSV 字符串。维度细分留在 JSON，CSV 只保留关键列。"""
    others = [m for m in models if m != focal]
    header = ["item_id", "skill", "skill_display", "category", "category_source",
              "difficulty", "has_ref", "question"]
    for m in models:
        header += [f"{m}_correctness", f"{m}_total", f"{m}_error_type", f"{m}_low_agreement"]
    for o in others:
        header.append(f"winrate_vs_{o}")
    header += ["meta_objective", "meta_judge", "meta_agree", "flags", "focal_answer"]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        pm = r["per_model"]
        row = [r["item_id"], r["skill"], r["skill_display"], "/".join(r["category"]),
               r["category_source"], r["difficulty"], r["has_ref"], r["question"]]
        for m in models:
            v = pm.get(m)
            if v:
                row += [v["correctness"], f"{v['total']:.2f}", v["error_type"] or "", v["low_agreement"]]
            else:
                row += ["", "", "", ""]
        for o in others:
            wr = r["pair_winrate_vs"].get(o)
            row.append(f"{wr:.3f}" if wr is not None else "")
        meta = r["meta"] or {}
        row += [meta.get("objective_correct", ""), meta.get("judge_correctness", ""),
                meta.get("agree", ""), ";".join(r["flags"]), r["focal_answer"]]
        w.writerow(row)
    return buf.getvalue()
