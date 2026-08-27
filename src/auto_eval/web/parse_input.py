"""输入解析：上传 jsonl / csv → 标准化题目列表。

每题返回 dict：
  compare:      {query, context?, video1, video2, answer1?, answer2?,
                 context1?, context2?, task_start_time?, task_end_time?}
  rich_content: {id?, query, context?, video_path, answer_text?,
                 task_start_time?, task_end_time?, category?}
"""
from __future__ import annotations

import csv
import io
import json
import math
from numbers import Real
from typing import Literal

Mode = Literal[
    "compare",
    "rich_content",
]


def _json_safe_source(value):
    """保留原始 JSONL 字段，同时把 NaN/Infinity 归一为空值。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe_source(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_source(item) for item in value]
    return value


# 旧字段名兼容读取：task_* 优先，缺省时回退到 content_*（历史数据集）
_RICH_CONTENT_TIME_LEGACY = {
    "task_start_time": "content_start_time",
    "task_end_time": "content_end_time",
}


def _rich_content_times(obj: dict) -> dict[str, float]:
    """读取垂域视觉评测视频的可选任务起止时间（单位：秒）。

    兼容旧的 ``content_start_time`` / ``content_end_time`` 字段：``task_*``
    优先，仅在 ``task_*`` 缺省时回退读取旧字段。
    """
    times: dict[str, float] = {}
    for field in ("task_start_time", "task_end_time"):
        value = obj.get(field)
        if value is None:
            value = obj.get(_RICH_CONTENT_TIME_LEGACY[field])
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        if normalized < 0:
            raise ValueError(f"{field} 不能小于 0")
        times[field] = normalized
    start = times.get("task_start_time", 0.0)
    end = times.get("task_end_time")
    if end is not None and end <= start:
        raise ValueError("task_end_time 必须大于 task_start_time")
    return times


def parse_text(text: str, mode: Mode) -> tuple[list[dict], list[str]]:
    """两种模式均需导入 JSONL/CSV（含视频路径），不支持文本粘贴解析。"""
    label_map = {"rich_content": "垂域视觉评测", "compare": "垂域视觉对比"}
    label = label_map.get(mode, "该")
    return [], [f"{label}评测请导入 JSONL（含视频路径），不支持文本粘贴解析"]


def parse_jsonl(content: str, mode: Mode) -> tuple[list[dict], list[str]]:
    """解析 jsonl；视频任务起止时间为可选秒数，空值使用默认策略。"""
    items: list[dict] = []
    errors: list[str] = []
    video_item_ids: set[str] = set()
    for ln, raw in enumerate(content.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"第 {ln} 行 JSON 错误：{e}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"第 {ln} 行必须是 JSON 对象")
            continue
        q = obj.get("question") or obj.get("query")
        if not isinstance(q, str) or not q.strip():
            errors.append(f"第 {ln} 行缺少 question")
            continue
        item: dict = {"query": q.strip()}
        context = obj.get("context")
        if context is not None and not isinstance(context, str):
            errors.append(f"第 {ln} 行 context 必须是字符串")
            continue
        if context and context.strip():
            item["context"] = context.strip()
        if mode == "compare":
            video1 = obj.get("video1")
            video2 = obj.get("video2")
            if not isinstance(video1, str) or not video1.strip():
                errors.append(f"第 {ln} 行 compare 模式缺少 video1")
                continue
            if not isinstance(video2, str) or not video2.strip():
                errors.append(f"第 {ln} 行 compare 模式缺少 video2")
                continue
            item["video1"] = video1.strip()
            item["video2"] = video2.strip()
            try:
                ct = _rich_content_times(obj)
            except ValueError as exc:
                errors.append(f"第 {ln} 行 {exc}")
                continue
            item.update(ct)
            for ctx_field in ("context1", "context2"):
                val = obj.get(ctx_field)
                if val is not None:
                    if not isinstance(val, str):
                        errors.append(f"第 {ln} 行 {ctx_field} 必须是字符串")
                        continue
                    if val.strip():
                        item[ctx_field] = val.strip()
            for ans_field in ("answer1", "answer2"):
                val = obj.get(ans_field)
                if val is not None:
                    if not isinstance(val, str):
                        errors.append(f"第 {ln} 行 {ans_field} 必须是字符串")
                        continue
                    if val.strip():
                        item[ans_field] = val.strip()
            if not obj.get("category"):
                item["category"] = "default"
            item["source_line"] = ln
            item_id = obj.get("id")
            if item_id is not None:
                if not isinstance(item_id, str) or not item_id.strip():
                    errors.append(f"第 {ln} 行 id 必须是非空字符串")
                    continue
                item_id = item_id.strip()
                if item_id in video_item_ids:
                    errors.append(f"第 {ln} 行 id 重复：{item_id}")
                    continue
                video_item_ids.add(item_id)
                item["id"] = item_id
        else:  # rich_content
            video_path = obj.get("video_path")
            if not isinstance(video_path, str) or not video_path.strip():
                errors.append(f"第 {ln} 行 {mode} 模式缺少 video_path")
                continue
            try:
                video_times = _rich_content_times(obj)
            except ValueError as exc:
                errors.append(f"第 {ln} 行 {exc}")
                continue
            item_id = obj.get("id")
            if item_id is not None:
                if not isinstance(item_id, str) or not item_id.strip():
                    errors.append(f"第 {ln} 行 id 必须是非空字符串")
                    continue
                item_id = item_id.strip()
                if item_id in video_item_ids:
                    errors.append(f"第 {ln} 行 id 重复：{item_id}")
                    continue
                video_item_ids.add(item_id)
                item["id"] = item_id
            item["video_path"] = video_path.strip()
            item["source_line"] = ln
            item.update(video_times)
            answer_text = obj.get("answer_text")
            if answer_text is not None and not isinstance(answer_text, str):
                errors.append(f"第 {ln} 行 answer_text 必须是字符串")
                continue
            item["category"] = obj.get("category") or "default"
            if answer_text and answer_text.strip():
                item["answer_text"] = answer_text.strip()
        if obj.get("category"):
            item["category"] = obj["category"]
        # 原始字段仅用于历史追溯和导出，不会进入 EvalItem 或裁判 prompt。
        item["source_data"] = _json_safe_source(obj)
        items.append(item)
    return items, errors


# --------------------------------------------------------------------------- #
# CSV 导入（垂域视觉评测多轮）
# --------------------------------------------------------------------------- #
_CSV_PLACEHOLDERS = {"", "n/a", "none", "null", "error"}


def _csv_clean(value) -> str:
    """清理 CSV 单元格：去首尾空白，占位值(N/A/None/Error/Null)→空串。"""
    if value is None:
        return ""
    v = str(value).strip()
    if v.lower() in _CSV_PLACEHOLDERS:
        return ""
    return v


def _csv_truthy(value) -> bool:
    """解析 is_start/is_end 等 TRUE/FALSE 标记。"""
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "y", "t")


def _csv_time(value, field: str, ln: int, errors: list[str]) -> float | None:
    """把 CSV 时间单元格解析为 float（秒）；非法时记 error 并返回 None。"""
    raw = _csv_clean(value)
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        errors.append(f"第 {ln} 行 {field} 不是有效数字：{raw!r}")
        return None


def _build_csv_context(start_time_node: str, location: str) -> str:
    """拼接时间/位置背景（与旧 csv_to_jsonl.build_context 一致）。"""
    parts: list[str] = []
    if start_time_node:
        parts.append(f"这个数据的产生时间：{start_time_node}")
    if location:
        parts.append(f"用户所在位置信息：{location}")
    return "；".join(parts)


def parse_csv(content: str, mode: Mode) -> tuple[list[dict], list[str]]:
    """解析垂域视觉评测多轮 CSV。

    仅支持 rich_content。**完全按 ``is_start``/``is_end``
    切 session**（不依赖上游 ``session_id`` 列）：每个 session 共享一段视频
    （``video_path`` 取 ``is_end`` 行的「文件路径」），每行（每轮 query）产出一条
    item，带上合成的 ``session_group``/``turn_index``，供 runner 组内串行评测、
    把前序轮次总结注入后续轮 context。

    必含列：``query``、``is_start``、``is_end``；时间列 ``开始时间``/``结束时间``
    与 ``文件路径`` 可为空（按默认策略）。前序轮次的总结不在解析阶段生成，
    而是在评测时产生（见 runner.run_session）。
    """
    if mode != "rich_content":
        return [], ["CSV 导入仅支持垂域视觉评测（rich_content）模式"]

    items: list[dict] = []
    errors: list[str] = []

    try:
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    except Exception as exc:  # noqa: BLE001
        return [], [f"CSV 解析失败：{exc}"]

    if not rows:
        return [], ["CSV 中没有数据行"]

    required = ("query", "is_start", "is_end")
    missing = [c for c in required if c not in fieldnames]
    if missing:
        return [], [
            "CSV 缺少必要列：" + "、".join(missing)
            + "（垂域视觉评测多轮 CSV 需含 index/query/is_start/is_end/"
            "开始时间/结束时间/文件路径 等列）"
        ]

    # —— 按 is_start/is_end 切 session（与上游 session_id 列无关）——
    # 每个 session = [(行号, row), ...]；行号从 2 起（1 为表头）。
    sessions: list[list[tuple[int, dict]]] = []
    current: list[tuple[int, dict]] | None = None
    for ln, row in enumerate(rows, start=2):
        is_start = _csv_truthy(row.get("is_start"))
        is_end = _csv_truthy(row.get("is_end"))
        if is_start or current is None:
            if current:
                sessions.append(current)  # 上一段未闭合，先收尾
            current = []
        current.append((ln, row))
        if is_end:
            sessions.append(current)
            current = None
    if current:
        sessions.append(current)

    group_counter = 0
    for session in sessions:
        group_id = f"csv-sess-{group_counter}"
        group_counter += 1
        # video_path：优先取 is_end 行（组内最后一行）的「文件路径」
        video_path = ""
        for _ln, row in reversed(session):
            vp = _csv_clean(row.get("文件路径"))
            if vp:
                video_path = vp
                break
        if not video_path:
            errors.append(f"第 {session[0][0]} 行起的 session 缺少「文件路径」，已跳过")
            continue

        for turn, (ln, row) in enumerate(session):
            query = _csv_clean(row.get("query"))
            if not query:
                errors.append(f"第 {ln} 行 query 为空，已跳过该轮")
                continue
            idx = (
                _csv_clean(row.get("index"))
                or _csv_clean(row.get("id"))
                or f"{group_id}-t{turn}"
            )
            answer_text = _csv_clean(row.get("回复内容"))
            context = _build_csv_context(
                _csv_clean(row.get("开始时间节点")),
                _csv_clean(row.get("位置信息")),
            )
            task_start = _csv_time(row.get("开始时间"), "开始时间", ln, errors)
            task_end = _csv_time(row.get("结束时间"), "结束时间", ln, errors)

            item: dict = {
                "id": idx,
                "query": query,
                "context": context,
                "video_path": video_path,
                "category": "default",
                "source_line": ln,
                "session_group": group_id,
                "turn_index": turn,
            }
            if answer_text:
                item["answer_text"] = answer_text
            if task_start is not None:
                item["task_start_time"] = task_start
            if task_end is not None:
                item["task_end_time"] = task_end
            item["source_data"] = _json_safe_source(row)
            items.append(item)

    return items, errors
