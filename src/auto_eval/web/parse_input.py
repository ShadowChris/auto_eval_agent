"""输入解析：粘贴文本 / 上传 jsonl → 标准化题目列表。

每题返回 dict：
  single : {query, answer, reference?}
  compare: {query, answer_a, answer_b, reference?}
  online : {query, reference?}
"""
from __future__ import annotations

import json
from typing import Literal

Mode = Literal["single", "compare", "online", "process"]


def parse_text(text: str, mode: Mode) -> tuple[list[dict], list[str]]:
    """解析 ||| 分隔的粘贴文本。返回 (items, errors)。"""
    items: list[dict] = []
    errors: list[str] = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|||")]
        try:
            if mode == "single":
                if len(parts) < 2:
                    raise ValueError("单评模式每行至少需 query ||| answer")
                item = {"query": parts[0], "answer": parts[1]}
                if len(parts) >= 3 and parts[2]:
                    item["competitor"] = parts[2]  # 第3段：竞品结果（产品专家用）
                if len(parts) >= 4 and parts[3]:
                    item["reference"] = parts[3]  # 第4段：参考答案
            elif mode == "compare":
                if len(parts) < 3:
                    raise ValueError("对比模式每行至少需 query ||| answerA ||| answerB")
                item = {"query": parts[0], "answer_a": parts[1], "answer_b": parts[2]}
                if len(parts) >= 4 and parts[3]:
                    item["reference"] = parts[3]
            elif mode == "online":
                if len(parts) < 1 or not parts[0]:
                    raise ValueError("在线模式每行至少需 query")
                item = {"query": parts[0]}
                if len(parts) >= 2 and parts[1]:
                    item["reference"] = parts[1]
            else:  # process
                if len(parts) < 3:
                    raise ValueError("过程模式每行至少需 query ||| answer ||| trace")
                item = {"query": parts[0], "answer": parts[1], "trace": parts[2]}
                if len(parts) >= 4 and parts[3]:
                    item["reference"] = parts[3]
            items.append(item)
        except ValueError as e:
            errors.append(f"第 {ln} 行：{e}（原文：{raw[:40]}）")
    return items, errors


def parse_jsonl(content: str, mode: Mode) -> tuple[list[dict], list[str]]:
    """解析 jsonl 文本。字段：question 必填；按 mode 取 answer/answer_a/answer_b/reference。"""
    items: list[dict] = []
    errors: list[str] = []
    for ln, raw in enumerate(content.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"第 {ln} 行 JSON 错误：{e}")
            continue
        q = obj.get("question") or obj.get("query")
        if not q:
            errors.append(f"第 {ln} 行缺少 question")
            continue
        item: dict = {"query": q}
        if mode == "single":
            a = obj.get("answer")
            if a is None:
                errors.append(f"第 {ln} 行 single 模式缺少 answer")
                continue
            item["answer"] = a
            if obj.get("competitor"):
                item["competitor"] = obj["competitor"]
        elif mode == "compare":
            aa = obj.get("answer_a") or obj.get("answerA")
            ab = obj.get("answer_b") or obj.get("answerB")
            if aa is None or ab is None:
                errors.append(f"第 {ln} 行 compare 模式缺少 answer_a/answer_b")
                continue
            item["answer_a"], item["answer_b"] = aa, ab
        elif mode == "process":
            a = obj.get("answer")
            tr = obj.get("trace")
            if a is None or tr is None:
                errors.append(f"第 {ln} 行 process 模式缺少 answer/trace")
                continue
            item["answer"], item["trace"] = a, tr
        # online 不需要 answer
        if obj.get("reference"):
            item["reference"] = obj["reference"]
        if obj.get("category"):
            item["category"] = obj["category"]
        items.append(item)
    return items, errors
