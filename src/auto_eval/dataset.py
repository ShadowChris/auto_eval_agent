"""评测集加载与 reference 隔离。

关键：`to_prompt()` 只返回 question+context，绝不包含 reference，
保证 Runner（作答）与 Judge（盲评）在结构上拿不到参考答案。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .schema import EvalItem


def load_dataset(path: str | Path, limit: int | None = None) -> list[EvalItem]:
    """从 jsonl 加载评测集并校验。"""
    path = Path(path)
    items: list[EvalItem] = []
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{ln} 非法 JSON：{e}") from e
            items.append(EvalItem(**obj))
            if limit and len(items) >= limit:
                break

    # 一致性校验：有 reference 的题 has_ref 应为 True
    for it in items:
        if it.reference and not it.has_ref:
            it.has_ref = True  # 自动修正
        if it.has_ref and not it.reference:
            # 有 has_ref 标记但缺 reference —— 视作无参考答案，避免误用
            it.has_ref = False
    return items


def to_prompt(item: EvalItem) -> str:
    """生成喂给被测模型/裁判的 prompt（严格不含 reference）。

    在这里集中控制作答格式，保证所有被测模型公平地拿到同样的输入。
    """
    parts: list[str] = []
    if item.context:
        parts.append("可信背景条件（由评测样本提供，请作为回答前提）：")
        parts.append(item.context.strip())
        parts.append("")
        parts.append("用户问题：")
    parts.append(item.question.strip())
    return "\n".join(parts).strip()


def assert_no_reference_leak(prompt: str, item: EvalItem) -> None:
    """防泄漏自检：prompt 中不应出现 reference 的原文（调试/测试用）。"""
    if item.reference and item.reference.strip() and item.reference.strip() in prompt:
        raise AssertionError(f"reference 泄漏到 prompt：item={item.id}")


def split_by_ref(items: list[EvalItem]) -> tuple[list[EvalItem], list[EvalItem]]:
    """按是否有参考答案分两桶。"""
    with_ref = [it for it in items if it.has_ref]
    without_ref = [it for it in items if not it.has_ref]
    return with_ref, without_ref


def category_distribution(items: list[EvalItem]) -> dict[str, int]:
    """类目分布统计，用于报告。"""
    c: Counter[str] = Counter()
    for it in items:
        for cat in it.categories():
            c[cat] += 1
    return dict(c)
