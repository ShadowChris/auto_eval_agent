"""任务类历史前缀生成，以及旧整会话历史的导出兼容。"""
from __future__ import annotations

import math
from typing import Any

from .schema import OperationTurn
from .query_images import attachment_image_urls

MAX_QUERY_IMAGES = 4
OPERATION_ROUND_FRAME_VERSION = "bounded-round-v3"


def operation_query_images(obj: dict) -> list[str]:
    """显式多图/单图优先，附件沿用域名/IP 地址规则，不读取 attachment_path。"""
    value = obj.get("query_images")
    if value is None or value == []:
        single = obj.get("query_image_path")
        if single is not None and not isinstance(single, str):
            raise ValueError("query_image_path 必须是图片路径或 HTTP/HTTPS 链接字符串")
        value = [single] if single and single.strip() else attachment_image_urls(obj)[:1]
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("query_images 必须是图片路径字符串数组")
    if len(value) > MAX_QUERY_IMAGES:
        raise ValueError(f"query_images 最多支持 {MAX_QUERY_IMAGES} 张")
    images: list[str] = []
    for index, path in enumerate(value, start=1):
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"query_images 第 {index} 项必须是非空图片路径")
        normalized = path.strip()
        if normalized not in images:
            images.append(normalized)
    return images


def marker_bool(value: Any) -> bool:
    """兼容 JSON 布尔值及 Excel/CSV 的 TRUE/FALSE、0/1。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        text = str(value).strip().lower()
        if text in {"true", "1", "1.0"}:
            return True
        if text in {"false", "0", "0.0"}:
            return False
    raise ValueError("is_start/is_end 必须是 TRUE/FALSE 或 1/0")


def has_turn_markers(row: dict) -> bool:
    return any(input_value_present(operation_input_field(row, key)) for key in ("is_start", "is_end"))


def operation_input_field(row: dict, key: str) -> Any:
    """Outer input wins, including explicit empty values; then metadata/source."""
    source = row.get("source_data")
    for container in (row, row.get("metadata"), source, source.get("metadata") if isinstance(source, dict) else None):
        if isinstance(container, dict) and key in container:
            return container[key]
    return None


def input_value_present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def has_session_markers(row: dict) -> bool:
    return any(input_value_present(operation_input_field(row, key)) for key in ("session_group", "turn_index"))


def session_turn_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("turn_index 必须是非负整数")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("turn_index 必须是非负整数") from None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError("turn_index 必须是非负整数")
    return int(number)


def session_group_key(row: dict) -> str | None:
    group = operation_input_field(row, "session_group")
    number = operation_input_field(row, "turn_index")
    if not input_value_present(group) or not input_value_present(number):
        return None
    if not isinstance(group, str):
        raise ValueError("session_group 必须是非空字符串")
    return group.strip()


def _source(row: dict) -> dict:
    source = dict(row["source_data"]) if isinstance(row.get("source_data"), dict) else dict(row)
    # API callers may supply source_data containing only business columns.
    # Keep the actual input markers before _round_item removes runtime markers.
    for key in ("is_start", "is_end", "session_group"):
        if key in row:
            source.setdefault(key, row[key])
    return source


def _group(rows: list[dict], *, parent: dict | None = None, fallback_id: str = "conversation_1") -> dict:
    first = rows[0]
    result = dict(parent or first)
    # Business case_id never determines a newly imported conversation. Preserve
    # canonical prefixes (including old histories) when normalizing them again.
    if parent and parent.get("evaluation_scope") == "latest_turn":
        conversation_id = parent.get("conversation_id") or first.get("id") or parent.get("case_id") or parent.get("id")
    else:
        conversation_id = first.get("id") or (parent or {}).get("id")
    conversation_id = str(conversation_id or fallback_id).strip()
    video = str(result.get("video_path") or first.get("video_path") or "").strip()
    if not video or video == "__missing_video__":
        raise ValueError("多轮 Case 首行或外层必须提供完整录屏 video_path")
    turns = []
    previous_end = -1.0
    for index, row in enumerate(rows, 1):
        row_video = str(row.get("video_path") or "").strip()
        if row_video not in ("", "__missing_video__", video):
            raise ValueError(f"第 {index} 轮 video_path 与完整录屏不一致")
        query = row.get("query") or row.get("question")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"第 {index} 轮缺少 query")
        answer = row.get("agent_statement", row.get("answer", "")) or ""
        if not isinstance(answer, str):
            raise ValueError(f"第 {index} 轮 answer 必须是字符串")
        context = row.get("context")
        if context is not None and not isinstance(context, str):
            raise ValueError(f"第 {index} 轮 context 必须是字符串")
        times = {}
        for key in ("task_start_time", "task_end_time"):
            value = row.get(key)
            if isinstance(value, bool) or value is None or value == "":
                raise ValueError(f"第 {index} 轮必须提供 {key}（完整视频的秒数）")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"第 {index} 轮 {key} 必须是有限数字") from None
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"第 {index} 轮 {key} 必须是非负有限数字")
            times[key] = number
        if times["task_end_time"] <= times["task_start_time"]:
            raise ValueError(f"第 {index} 轮结束时间必须大于开始时间")
        if times["task_start_time"] < previous_end:
            raise ValueError(f"第 {index} 轮时间区间与前一轮重叠或乱序")
        previous_end = times["task_end_time"]
        turn = OperationTurn(
            id=str(row.get("id") or ""), query=query.strip(), answer=answer,
            context=context, query_images=operation_query_images(row),
            turn_index=index, source_data=_source(row), **times,
            source_line=row.get("source_line"),
        )
        turns.append(turn.model_dump())
    result.update({
        "id": conversation_id, "conversation_id": conversation_id,
        "case_id": result.get("case_id", conversation_id), "turns": turns,
        "turn_count": len(turns), "video_path": video,
        "query": "\n".join(f"第{t['turn_index']}轮：{t['query']}" for t in turns),
        "answer": "\n".join(f"第{t['turn_index']}轮：{t['answer']}" for t in turns),
        "task_start_time": turns[0]["task_start_time"],
        "task_end_time": turns[-1]["task_end_time"],
        "source_data": _source(result if parent else first),
    })
    # Context and images remain round-scoped, never promoted into earlier turns.
    for key in ("context", "query_images", "query_image_path", "attachment_path", "agent_statement"):
        result.pop(key, None)
    result.pop("question", None)
    return result


def group_operation_items(items: list[dict]) -> list[dict]:
    """Accept legacy single items, contiguous marker rows, or structured turns.

    Invalid group structure rejects the submission rather than silently dropping a
    round and joining unrelated rows. Safe to call again on parsed items.
    """
    grouped: list[dict] = []
    pending: list[dict] = []
    for position, original in enumerate(items, 1):
        if not isinstance(original, dict):
            raise ValueError(f"第 {position} 行必须是对象")
        row = dict(original)
        if row.get("evaluation_scope") == "latest_turn":
            if pending:
                raise ValueError("上一会话尚未结束，不能混入逐轮评估项")
            turns = row.get("turns")
            if (not isinstance(turns, list) or not turns or not all(isinstance(t, dict) for t in turns)
                    or isinstance(row.get("turn_index"), bool) or row.get("turn_index") != len(turns)):
                raise ValueError("逐轮输入的 turns 必须恰好包含截至当前轮的前缀，不得含未来轮次")
            validated = _group(turns, parent=row)
            count = row.get("turn_count", len(turns))
            if isinstance(count, bool) or not isinstance(count, int) or count < len(turns):
                raise ValueError("turn_count 必须是大于等于当前轮次的整数")
            normalized = _round_item(validated, len(turns) - 1, turn_count=count)
            if row.get("id"):
                normalized["id"] = row["id"]
            normalized["source_data"] = _source(row)
            if "case_id" in normalized["source_data"]:
                normalized["case_id"] = normalized["source_data"]["case_id"]
            grouped.append(normalized)
            continue
        if row.get("turns") is not None:
            if pending:
                raise ValueError(f"第 {position} 行之前的多轮缺少 is_end")
            turns = row["turns"]
            if not isinstance(turns, list) or not turns or not all(isinstance(t, dict) for t in turns):
                raise ValueError("turns 必须是非空对象数组")
            grouped.append(_group(turns, parent=row, fallback_id=f"conversation_{len(grouped) + 1}"))
            continue
        if not has_turn_markers(row):
            if pending:
                raise ValueError(f"第 {position} 行缺少多轮标记，上一会话尚未结束")
            grouped.append(row)
            continue
        try:
            start, end = marker_bool(operation_input_field(row, "is_start")), marker_bool(operation_input_field(row, "is_end"))
        except ValueError as exc:
            raise ValueError(f"第 {position} 行：{exc}") from exc
        if start:
            if pending:
                raise ValueError(f"第 {position} 行重复 is_start，上一会话尚未结束")
            if end:
                grouped.append(row)
                continue
            pending = [row]
        elif not pending:
            raise ValueError(f"第 {position} 行缺少对应的 is_start")
        else:
            pending.append(row)
        if end:
            grouped.append(_group(pending, fallback_id=f"conversation_{len(grouped) + 1}"))
            pending = []
    if pending:
        raise ValueError("多轮 Case 缺少结束行 is_end=TRUE")
    identifiers = [str(row.get("id") or "").strip() for row in grouped]
    present = [key for key in identifiers if key]
    if len(present) != len(set(present)):
        raise ValueError("归组后的 Case id 重复")
    return grouped


def _round_item(conversation: dict, index: int, *, turn_count: int | None = None) -> dict:
    """One independently evaluated row, carrying only its past/current inputs."""
    import copy

    turns = conversation["turns"]
    current = turns[index]
    conversation_id = conversation.get("conversation_id") or conversation["case_id"]
    source_data = copy.deepcopy(current.get("source_data") or {})
    parent_source = conversation.get("source_data") or {}
    if "case_id" not in source_data and "turns" in parent_source and "case_id" in parent_source:
        source_data["case_id"] = copy.deepcopy(parent_source["case_id"])
    if "session_group" not in source_data and "turns" in parent_source and "session_group" in parent_source:
        source_data["session_group"] = copy.deepcopy(parent_source["session_group"])
    row = dict(conversation)
    row.update({
        "id": current.get("id") or f"{conversation_id}_turn_{index + 1:03d}",
        "conversation_id": conversation_id,
        "case_id": source_data.get("case_id", conversation_id),
        "query": current["query"], "answer": current.get("answer") or "",
        "context": current.get("context"), "query_images": list(current.get("query_images") or []),
        "source_data": source_data,
        "task_start_time": current["task_start_time"], "task_end_time": current["task_end_time"],
        "turns": copy.deepcopy(turns[:index + 1]), "turn_index": index + 1,
        "turn_count": turn_count or len(turns), "evaluation_scope": "latest_turn",
        "source_line": current.get("source_line") or conversation.get("source_line"),
    })
    row["category"] = row.get("category") or "operation"
    row.pop("session_group", None)
    if "session_group" in source_data:
        row["session_group"] = source_data["session_group"]
    for field in ("is_start", "is_end", "is_result_row", "question", "agent_statement", "query_image_path", "attachment_path"):
        row.pop(field, None)
    if not row.get("context"):
        row.pop("context", None)
    if not row.get("query_images"):
        row.pop("query_images", None)
    return row


def normalize_operation_items(items: list[dict]) -> list[dict]:
    """New submissions evaluate each round; unmarked single rows are unchanged."""
    if any(isinstance(row, dict) and row.get("evaluation_scope") != "latest_turn"
           and row.get("turns") is None and has_session_markers(row) for row in items):
        normalized = normalize_operation_batch(items)
        errors = [row["input_error"] for row in normalized if row.get("input_error")]
        if errors:
            raise ValueError("；".join(dict.fromkeys(errors)))
        return normalized
    output = []
    for item in group_operation_items([_canonical_round_input(row) for row in items]):
        if item.get("evaluation_scope") == "latest_turn" or not item.get("turns"):
            output.append(item)
        else:
            output.extend(_round_item(item, index) for index in range(len(item["turns"])))
    identifiers = [item.get("id") for item in output if item.get("id")]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("逐轮 Case id 重复，请为每个原始轮次提供唯一 id")
    validate_round_prefixes(output)
    return output


def _canonical_round_input(row: dict) -> dict:
    """JSONL keeps the external zero-based counter separate from runtime ordinal."""
    run = row.get("eval_run") if isinstance(row, dict) else None
    if isinstance(run, dict) and row.get("evaluation_scope") == "latest_turn" and "turn_index" in run:
        return {**row, "source_data": _source(row), "turn_index": run["turn_index"]}
    return row


def _failed_operation_input(row: dict, error: str, **fields: Any) -> dict:
    result = dict(row)
    result.setdefault("source_data", _source(row))
    result.update(fields, input_error=error)
    result["query"] = str(row.get("query") or row.get("question") or "")
    result["video_path"] = str(row.get("video_path") or "")
    for field in ("answer", "context"):
        if not isinstance(result.get(field), str):
            result.pop(field, None)
    result.pop("turns", None)
    return result


def _normalize_session_group_items(items: list[dict]) -> list[dict] | None:
    """Group by explicit key, build chronological prefixes, retain source order."""
    groups: dict[str, list[tuple[int, dict]]] = {}
    selected: set[int] = set()
    placed: list[tuple[int, int, dict]] = []
    for position, original in enumerate(items):
        if not isinstance(original, dict) or original.get("turns") is not None or original.get("evaluation_scope") == "latest_turn":
            continue
        try:
            key = session_group_key(original)
        except ValueError as exc:
            selected.add(position)
            placed.append((position, 0, _failed_operation_input(original, str(exc))))
            continue
        if key is None:
            continue
        row = dict(original)
        row.setdefault("source_line", position + 1)
        row["source_data"] = _source(original)
        for field in ("session_group", "turn_index"):
            row["source_data"][field] = operation_input_field(original, field)
        groups.setdefault(key, []).append((position, row))
        selected.add(position)
    if not selected:
        return None
    from collections import Counter

    for key, members in groups.items():
        invalid = ""
        try:
            ordered = sorted(members, key=lambda entry: session_turn_number(operation_input_field(entry[1], "turn_index")))
        except ValueError as exc:
            ordered, invalid = members, str(exc)
        numbers = [] if invalid else [session_turn_number(operation_input_field(row, "turn_index")) for _, row in ordered]
        counts = Counter(numbers)
        expected = 0
        turns = []
        for index, (_, row) in enumerate(ordered):
            turn = dict(row)
            if invalid:
                turn["input_error"] = f"会话 {key} 的轮次无法可靠排序：{invalid}"
            else:
                number = numbers[index]
                if counts[number] > 1:
                    turn["input_error"] = f"会话 {key} 的 turn_index={number} 重复，无法确定该轮历史"
                elif number != expected:
                    turn["input_error"] = f"会话 {key} 缺少 turn_index={expected} 的历史，不能跳过轮次评估"
                expected = number + 1
            turns.append(turn)
        normalized = normalize_operation_batch([{"turns": turns}])
        for (position, _), item in zip(ordered, normalized):
            item["conversation_id"] = f"session_group:{key}"
            if "case_id" not in (item.get("source_data") or {}):
                item["case_id"] = item["conversation_id"]
            placed.append((position, 0, item))
    position = 0
    while position < len(items):
        if position in selected:
            position += 1
            continue
        start = position
        while position < len(items) and position not in selected:
            position += 1
        segment = [{**row, "source_line": row.get("source_line", start + offset + 1)}
                   if isinstance(row, dict) else row for offset, row in enumerate(items[start:position])]
        placed.extend((start, index, item) for index, item in enumerate(normalize_operation_batch(segment)))
    return [item for _, _, item in sorted(placed, key=lambda entry: entry[:2])]


def _serializable_operation_input(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _serializable_operation_input(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_serializable_operation_input(child) for child in value]
    return value


def normalize_operation_batch(items: list[dict]) -> list[dict]:
    """Recover batch boundaries without discarding rows or relaxing evidence checks.

    The strict normalizer above remains useful for validating a single explicit
    prefix. Here a bad round invalidates its dependent prefixes, not other cases.
    Failed rows never contain partially valid ``turns`` for a judge to consume.
    """
    items = [_serializable_operation_input(item) for item in items]
    for row in items:
        if not isinstance(row, dict) or row.get("turns") is not None or row.get("evaluation_scope") == "latest_turn":
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        supplied = [key for key in ("session_group", "turn_index") if key in row or key in metadata]
        if supplied:
            source = _source(row)
            source.update({key: operation_input_field(row, key) for key in supplied})
            row["source_data"] = source
    session_items = _normalize_session_group_items(items)
    if session_items is not None:
        return _validate_batch_identities(session_items)
    output: list[dict] = []
    pending: list[dict] = []

    failed = _failed_operation_input

    def emit(rows: list[dict], *, parent: dict | None = None, warning: str = "") -> None:
        first = rows[0]
        conversation_id = str((parent or {}).get("conversation_id") or first.get("id")
                              or (parent or {}).get("id") or f"conversation_row_{len(output) + 1}")
        # A complete recording may be provided on any round, but never guess
        # between different recordings or replace invalid time windows.
        candidates = [row.get("video_path") for row in [parent or {}, *rows]]
        videos = {v.strip() for v in candidates if isinstance(v, str) and v.strip()
                  and v.strip() != "__missing_video__"}
        common_error = ""
        if any(v is not None and not isinstance(v, str) for v in candidates):
            common_error = "多轮 video_path 必须是字符串"
        elif not videos:
            common_error = "多轮会话未提供完整录屏 video_path"
        elif len(videos) > 1:
            common_error = "多轮会话存在不同的 video_path，无法确定完整录屏"
        video = next(iter(videos)) if len(videos) == 1 else ""
        warnings = [warning] if warning else []
        if video and str(first.get("video_path") or "").strip() in ("", "__missing_video__") and not (parent or {}).get("video_path"):
            warnings.append("首轮未提供录屏，已复用本会话其他轮唯一的完整录屏 video_path")
        first_error = common_error
        bad_index = 1 if common_error else 0
        for index, row in enumerate(rows, 1):
            fields = dict(conversation_id=conversation_id, turn_index=index,
                          turn_count=len(rows), evaluation_scope="latest_turn")
            try:
                if first_error:
                    raise ValueError(first_error if index == bad_index else
                                     f"依赖第 {bad_index} 轮无效输入，无法评估当前历史前缀：{first_error}")
                if row.get("input_error"):
                    raise ValueError(row["input_error"])
                # Validate all preceding rounds, including their original images.
                conversation = _group(rows[:index], parent={**(parent or {}), "video_path": video},
                                      fallback_id=conversation_id)
                conversation["conversation_id"] = conversation_id
                item = _round_item(conversation, index - 1, turn_count=len(rows))
            except (ValueError, TypeError) as exc:
                if not first_error:
                    first_error, bad_index = str(exc), index
                item = failed(row, str(exc), **fields)
                item["id"] = row.get("id") or f"{conversation_id}_turn_{index:03d}"
                item["video_path"] = video or item["video_path"]
            if warnings:
                item["input_warnings"] = list(dict.fromkeys([*(item.get("input_warnings") or []), *warnings]))
            output.append(item)

    def flush(warning: str = "") -> None:
        if pending:
            emit(pending, warning=warning)
            pending.clear()

    for position, original in enumerate(items, 1):
        row = dict(original) if isinstance(original, dict) else {
            "source_data": {"raw_value": original}, "input_error": "输入行必须是 JSON 对象",
        }
        row.setdefault("source_line", position)
        if row.get("evaluation_scope") == "latest_turn" or row.get("turns") is not None:
            row = _canonical_round_input(row)
            flush("上一会话缺少 is_end=TRUE，已在下一条独立输入前结束")
            if row.get("input_error"):
                output.append(row)
            elif row.get("evaluation_scope") == "latest_turn":
                try:
                    output.extend(normalize_operation_items([row]))
                except (ValueError, TypeError) as exc:
                    output.append(failed(row, f"当前轮历史前缀无效：{exc}"))
            else:
                turns = row.get("turns")
                if not isinstance(turns, list) or not turns or not all(isinstance(t, dict) for t in turns):
                    output.append(failed(row, "turns 必须是非空对象数组"))
                else:
                    emit(turns, parent=row)
            continue
        if not has_turn_markers(row) and not pending:
            output.append(failed(row, "session_group/turn_index 不完整，且未提供 is_start/is_end，无法确定会话")
                          if has_session_markers(row) else row)
            continue
        try:
            start = marker_bool(operation_input_field(row, "is_start"))
        except ValueError:
            start = None
        # An explicit new start is a reliable boundary even if its end marker
        # is malformed. Never attach it to the preceding conversation.
        if start is True:
            flush("上一会话缺少 is_end=TRUE，已在下一条 is_start=TRUE 前结束")
        try:
            if start is None:
                raise ValueError("is_start/is_end 必须是 TRUE/FALSE 或 1/0")
            end = marker_bool(operation_input_field(row, "is_end"))
        except ValueError as exc:
            row = failed(row, str(row.get("input_error") or f"多轮标记无效：{exc}"))
            if pending or start is True:
                pending.append(row)
            else:
                output.append(row)
            continue
        if start:
            if end:
                output.append(row)  # Explicit standalone row keeps single-round defaults.
                continue
            pending.append(row)
        elif not pending:
            output.append(failed(row, "缺少对应的 is_start=TRUE，无法确定本轮历史上下文"))
            continue
        else:
            pending.append(row)
        if end:
            flush()
    flush("文件末尾缺少 is_end=TRUE，已按现有连续轮次结束会话")

    return _validate_batch_identities(output)


def _validate_batch_identities(output: list[dict]) -> list[dict]:
    """Do not let ambiguous execution ids overwrite one another in histories."""
    from collections import Counter

    counts = Counter(str(row.get("id")) for row in output if row.get("id"))
    reserved = set(counts)
    seen: dict[tuple[str, int], tuple] = {}
    for position, row in enumerate(output, 1):
        if row.get("id") and counts[str(row["id"])] > 1:
            original_id = row["id"]
            row = _failed_operation_input(row, f"id 重复：{original_id}；请修正原始 id 后重跑")
            identifier = f"__invalid_row_{position}_{original_id}"
            while identifier in reserved:
                identifier += "_"
            reserved.add(identifier)
            row["id"] = identifier
            output[position - 1] = row
        if not row.get("input_error"):
            try:
                validate_round_prefixes([row], _seen=seen)
            except (ValueError, TypeError) as exc:
                output[position - 1] = _failed_operation_input(row, str(exc))
    return output


def validate_round_prefixes(items: list[dict], *, _seen: dict | None = None) -> None:
    """Reject conflicting historical inputs, including partial append/overwrite."""
    seen: dict[tuple[str, int], tuple] = {} if _seen is None else _seen
    for item in items:
        if item.get("evaluation_scope") != "latest_turn":
            continue
        updates: dict[tuple[str, int], tuple] = {}
        for index, turn in enumerate(item.get("turns") or [], 1):
            original = turn.get("source_data") or turn
            signature = (turn.get("query"), turn.get("answer") or "", turn.get("context") or "",
                tuple(operation_query_images(original)), turn.get("task_start_time"), turn.get("task_end_time"))
            first_id = (item.get("turns") or [{}])[0].get("id")
            key = (str(item.get("conversation_id") or first_id or item.get("case_id") or item.get("id")), index)
            if key in seen and seen[key] != signature:
                raise ValueError(f"会话 {key[0]} 第 {index} 轮历史输入不一致；修改前轮时请同步替换受影响后轮的前缀，或新建评估集")
            updates[key] = signature
        # Invalid prefixes must not bind history for a subsequent valid item.
        seen.update(updates)


def conversation_export_snapshot(snapshot: dict) -> dict:
    """Expand cases to source rows for exports only, not for execution/statistics."""
    if not any(item.get("turns") and item.get("evaluation_scope") != "latest_turn" for item in snapshot.get("items") or []):
        return snapshot
    items, results = [], []
    progress = {}
    by_index = {int(r.get("index", position)): r for position, r in enumerate(snapshot.get("results") or []) if str(r.get("index", position)).isdigit()}
    for index, item in enumerate(snapshot.get("items") or []):
        if item.get("evaluation_scope") == "latest_turn":
            # New histories already have one item/result per source row.
            new_index = len(items)
            items.append(item)
            if index in by_index:
                results.append({**by_index[index], "index": new_index})
            progress[str(new_index)] = (snapshot.get("item_progress") or {}).get(str(index), {})
            continue
        turns = item.get("turns") or []
        source_turns = turns or [item]
        case_id = item.get("case_id") or item.get("id") or f"q{index}"
        for turn_index, turn in enumerate(source_turns, 1):
            row = dict(item)
            row.pop("turns", None)
            if turns:
                row.update({key: turn.get(key) for key in (
                    "id", "query", "answer", "context", "query_images", "task_start_time", "task_end_time", "source_data",
                )})
            row.update(case_id=case_id, turn_index=turn_index, turn_count=len(source_turns), is_result_row=turn_index == 1)
            row["source_data"] = {
                **_source(turn), "case_id": case_id, "turn_index": turn_index,
                "turn_count": len(source_turns), "is_result_row": turn_index == 1,
            }
            items.append(row)
            if turn_index == 1:
                current_progress = snapshot.get("item_progress") or {}
                progress[str(len(items) - 1)] = current_progress.get(str(index)) or current_progress.get(index) or {}
                if index not in by_index:
                    continue
            result = dict(by_index.get(index) or {}) if turn_index == 1 else {}
            result.pop("数据集序号", None)
            result.update(index=len(items) - 1, item_id=row.get("id") or case_id, query=row.get("query", ""), answer=row.get("answer", ""), context=row.get("context") or "")
            if turns:
                result.update(query_images=turn.get("query_images") or [], query_image_count=len(turn.get("query_images") or []))
            results.append(result)
    return {**snapshot, "items": items, "results": results, "item_progress": progress}


def fold_conversation_export_rows(records: list[dict]) -> list[dict]:
    """Read row-aligned exports as cases; continuation blanks are not failures."""
    if not any(row.get("is_result_row") not in (None, "") for row in records):
        return records
    output = []
    position = 0
    while position < len(records):
        first = records[position]
        if first.get("evaluation_scope") == "latest_turn":
            output.append(first)
            position += 1
            continue
        if first.get("is_result_row") in (None, ""):
            output.append(first)
            position += 1
            continue
        if not marker_bool(first["is_result_row"]):
            raise ValueError("多轮结果缺少首轮结果行")
        try:
            count = int(first.get("turn_count") or 1)
        except (TypeError, ValueError):
            raise ValueError("turn_count 必须是正整数") from None
        if count < 1 or position + count > len(records):
            raise ValueError("多轮结果的轮数不完整")
        group = records[position:position + count]
        case_id = str(first.get("case_id") or first.get("id") or "").strip()
        for index, row in enumerate(group, 1):
            if str(row.get("case_id") or row.get("id") or "").strip() != case_id:
                raise ValueError("多轮结果 case_id 不一致")
            if int(row.get("turn_index") or 1) != index or marker_bool(row.get("is_result_row")) != (index == 1):
                raise ValueError("多轮结果的轮次或结果行标记无效")
        result = dict(first)
        if count > 1:
            result.update(id=case_id, query="\n".join(f"第{i}轮：{r.get('query', '')}" for i, r in enumerate(group, 1)), turns=group)
        output.append(result)
        position += count
    return output
