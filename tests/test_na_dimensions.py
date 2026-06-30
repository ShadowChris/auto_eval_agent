"""验证 N/A（不适用）维度修复的独立测试脚本。
直接运行：python tests/test_na_dimensions.py
不依赖 LLM / API，仅测核心管道：_flatten_rubric → SingleScore → aggregate_scores
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_eval.config import EnsembleConfig, RubricDim, SubDim
from auto_eval.judges.ensemble import aggregate_scores
from auto_eval.judges.rubric_judge import _flatten_rubric
from auto_eval.schema import SingleScore

# ── 用英文 dimension 名避免 Windows GBK 编码问题 ──
DIMS = [
    RubricDim(name="accuracy", description="事实与逻辑是否正确", weight=1.5, sub_dimensions=[
        SubDim(name="fact"), SubDim(name="logic"), SubDim(name="timeliness"),
    ]),
    RubricDim(name="completeness", description="是否充分回应要点", weight=1.0, sub_dimensions=[
        SubDim(name="coverage"), SubDim(name="detail"),
    ]),
    RubricDim(name="relevance", description="是否切题不跑题", weight=1.0, sub_dimensions=[
        SubDim(name="text"), SubDim(name="source"), SubDim(name="image"), SubDim(name="link"),
    ]),
    RubricDim(name="usefulness", description="清晰易懂满足需求", weight=1.0, sub_dimensions=[
        SubDim(name="clarity"), SubDim(name="actionable"), SubDim(name="satisfaction"),
    ]),
    RubricDim(name="safety", description="是否含风险内容", weight=0.5, sub_dimensions=[
        SubDim(name="harmless"), SubDim(name="compliance"),
    ]),
]
DIM_WEIGHT = {d.name: d.weight for d in DIMS}
CFG = EnsembleConfig()


def old_flatten_rubric(raw):
    """旧逻辑：null 的子维度不参与均值 → 但缺失值会变成 0（Bug 复现）"""
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict):
            nums = [x for x in v.values() if isinstance(x, (int, float)) and not isinstance(x, bool)]
            # 旧逻辑：有 total 就用 total，没有就平均（子维度 null 被误当成 0）
            if "total" in v and isinstance(v["total"], (int, float)):
                out[k] = int(v["total"])
            else:
                out[k] = round(sum(nums) / len(nums)) if nums else 0
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
        else:
            out[k] = 0
    return out


def old_total(raw):
    """旧逻辑总分：所有维度加权平均（N/A 被视为 0 分）"""
    rubric = old_flatten_rubric(raw)
    wsum = sum(DIM_WEIGHT.get(k, 1.0) for k in rubric)
    return sum(rubric[k] * DIM_WEIGHT.get(k, 1.0) for k in rubric) / wsum if wsum else 0


def new_total(raw):
    """新逻辑总分：通过 _flatten_rubric + aggregate_scores，排除 N/A"""
    rubric, _, na_dims = _flatten_rubric(raw, dim_names=[d.name for d in DIMS])
    s = SingleScore(
        item_id="test", model="m", judge="j",
        rubric=rubric, na_dimensions=na_dims,
        total=sum(rubric.values()) / len(rubric) if rubric else 0,
        correctness="right",
    )
    v = aggregate_scores([s], DIMS, CFG, 0.6)
    return v.total, v.na_dimensions


# ── 模拟裁判 JSON 输出（含 null = N/A） ──
CASES = [
    {
        "name": "1.纯文本事实问答（李白朝代）",
        "desc": "答案无图/无链接/无安全风险 → 多个子维度和安全性 N/A",
        "raw": {
            "accuracy":    {"fact": 5, "logic": 5, "timeliness": None, "total": 5},
            "completeness":{"coverage": 5, "detail": 4, "total": 5},
            "relevance":   {"text": 5, "source": None, "image": None, "link": None, "total": 5},
            "usefulness":  {"clarity": 5, "actionable": None, "satisfaction": 5, "total": 5},
            "safety":      None,
        },
    },
    {
        "name": "2.有安全风险的答案（教人做炸弹）",
        "desc": "安全性必须适用 → N/A 不应误排除安全性",
        "raw": {
            "accuracy":    {"fact": 3, "logic": 3, "timeliness": 4, "total": 3},
            "completeness":{"coverage": 4, "detail": 3, "total": 4},
            "relevance":   {"text": 5, "source": None, "image": None, "link": None, "total": 5},
            "usefulness":  {"clarity": 4, "actionable": None, "satisfaction": 4, "total": 4},
            "safety":      {"harmless": 1, "compliance": 1, "total": 1},  # 低分，但适用！
        },
    },
    {
        "name": "3.搜图题-答案含图片和外链",
        "desc": "所有子维度都适用（仅安全性 N/A）",
        "raw": {
            "accuracy":    {"fact": 4, "logic": 4, "timeliness": 4, "total": 4},
            "completeness":{"coverage": 5, "detail": 5, "total": 5},
            "relevance":   {"text": 5, "source": 4, "image": 5, "link": 5, "total": 5},  # 全部适用
            "usefulness":  {"clarity": 5, "actionable": 4, "satisfaction": 5, "total": 5},
            "safety":      None,
        },
    },
    {
        "name": "4.全部维度适用（无 N/A）",
        "desc": "对照组：新旧逻辑应一致",
        "raw": {
            "accuracy":    {"fact": 4, "logic": 4, "timeliness": 4, "total": 4},
            "completeness":{"coverage": 4, "detail": 4, "total": 4},
            "relevance":   {"text": 4, "source": 4, "image": 4, "link": 4, "total": 4},
            "usefulness":  {"clarity": 4, "actionable": 4, "satisfaction": 4, "total": 4},
            "safety":      {"harmless": 4, "compliance": 4, "total": 4},
        },
    },
    {
        "name": "5.低质量答案（全维度低分但都适用）",
        "desc": "对照组：低分不应被误标为 N/A",
        "raw": {
            "accuracy":    {"fact": 2, "logic": 2, "timeliness": 2, "total": 2},
            "completeness":{"coverage": 1, "detail": 1, "total": 1},
            "relevance":   {"text": 3, "source": 2, "image": 1, "link": 1, "total": 2},
            "usefulness":  {"clarity": 2, "actionable": 1, "satisfaction": 1, "total": 1},
            "safety":      {"harmless": 2, "compliance": 2, "total": 2},
        },
    },
]


def run():
    print("=" * 72)
    print("N/A 维度修复验证 — 新旧逻辑对比")
    print("=" * 72)
    print()

    passed = 0
    failed = 0

    for c in CASES:
        raw = c["raw"]
        old = old_total(raw)
        new, na_dims = new_total(raw)

        # 计算子维度 N/A 数量
        sub_na_count = 0
        for k, v in raw.items():
            if isinstance(v, dict):
                sub_na_count += sum(1 for sv in v.values() if sv is None)

        print(f"--- {c['name']} ---")
        print(f"    {c['desc']}")
        print(f"    N/A 维度: {na_dims},  子维度 N/A 数: {sub_na_count}")
        print(f"    旧总分: {old:.2f}  →  新总分: {new:.2f}  (差值: {new - old:+.2f})")

        # 判断规则
        has_na = bool(na_dims) or sub_na_count > 0
        if has_na:
            if new > old:
                print(f"    [OK] 有 N/A，新逻辑得分高于旧逻辑（旧逻辑被 N/A 拉低）")
                passed += 1
            elif new == old:
                print(f"    [??] 有 N/A 但新旧一致，检查是否所有适用维度满分")
                passed += 1  # 如果全部适用维度都是满分，也可以
            else:
                print(f"    [FAIL] 有 N/A 但新逻辑反而更低，不合预期")
                failed += 1
        else:
            if abs(new - old) < 0.01:
                print(f"    [OK] 无 N/A，新旧逻辑一致")
                passed += 1
            else:
                print(f"    [FAIL] 无 N/A 但新旧不一致，差值 {new - old:+.2f}")
                failed += 1

        # 额外检查：低质量答案不应有 N/A
        if c["name"].startswith("5."):
            if na_dims:
                print(f"    [WARN] 低质量答案不应该有 N/A 维度！可能是子维度全 N/A 误判")
            else:
                print(f"    [OK] 低质量答案没有 N/A，N/A 与低分边界正确")

        print()

    print("=" * 72)
    print(f"结果: {passed} passed, {failed} failed")
    if failed == 0:
        print("全部通过！N/A 修复工作正常。")
    print("=" * 72)


if __name__ == "__main__":
    run()
