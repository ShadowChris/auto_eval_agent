"""任务类（录屏）裁判输出字段的结构归一化。"""
from __future__ import annotations

import re
from typing import Any

from ..schema import OperationCard, OperationCorrectness, OperationRouteEvidence

_VALID_CORRECTNESS = {"ok", "nok", "no_support", "others"}
_VALID_ROUTE_STATUS = {"detected", "uncertain", "insufficient_evidence"}
_ROUTE_ALIASES = {
    "fast_system": "fast_system",
    "fast system": "fast_system",
    "快系统": "fast_system",
    "任务快系统": "fast_system",
    "skill": "skill",
    "技能": "skill",
    "技能链路": "skill",
    "jarvis": "jarvis",
    "贾维斯": "jarvis",
    "other": "other",
    "others": "other",
    "其他": "other",
    "qa": "other",
    "问答": "other",
}
_ROUTE_DISPLAY = {
    "fast_system": "快系统",
    "skill": "skill",
    "jarvis": "贾维斯",
    "other": "其他",
}
_GENERIC_TOOL_RETURN_CUES = ("相关工具已返回结果", "工具已返回结果")
_STRONG_SKILL_EVIDENCE_CUES = (
    "step info",
    "步骤信息",
    "步骤提示",
    "步骤数量",
    "齿轮",
    "可展开",
    "已完成栏",
    "完成面板",
)
_JARVIS_EMBEDDED_STEP_CUES = ("任务执行中", "应用操作")
_INDEPENDENT_SKILL_CUES = ("调用", "已完成栏", "完成面板", "齿轮", "可展开")
_OPERATION_ROUTES = {"fast_system", "skill", "jarvis"}
_INDEPENDENT_OTHER_CUES = (
    "生成回复",
    "参考",
    "篇资料",
    "引用来源",
    "联网检索",
    "检索生成",
    "问答卡片",
    "独立qa",
    "独立 qa",
)
QUERY_ALIGNMENT_ISSUE = "录屏Query无法与输入Query一致核验"
FACT_REVIEW_CATEGORY = "关键事实待核验"
LEGACY_REVIEW_CATEGORIES = {
    "待用户澄清", "缺少前置条件", "其他阻塞原因", "未展示可验证结果",
    "环境或算力异常", "外部服务暂时异常", "视频损坏", "录屏数据不完整",
    "视频无法解析", "任务意图不明确", "任务要求矛盾", "输入数据异常",
    QUERY_ALIGNMENT_ISSUE, "任务执行窗口未展开", "评测证据冲突",
    "未预期场景", "其他未归类情况", FACT_REVIEW_CATEGORY,
}
_LEGACY_CORRECTNESS = {
    "right": "ok",
    "partial": "ok",
    "wrong": "nok",
}
_NO_SUPPORT_HINTS = (
    "权限",
    "授权",
    "登录",
    "信息补充",
    "待用户澄清",
    "缺少前置条件",
    "身份验证",
    "敏感操作",
    "应用未安装",
    "目标对象不存在",
    "不具备完成条件",
    "设备",
    "硬件",
    "外部服务",
)
_ALIASES = {
    "路径错误": "任务结果错误",
    "单任务未完成": "应执行目标未执行",
    "任务未完成": "应执行目标未执行",
    "多任务未全部完成": "应执行目标未执行",
    "最终步骤未执行": "应执行目标未执行",
    "仅文字无状态证据": "未展示可验证结果",
    "仅文字声称完成": "未展示可验证结果",
    "完成证据不足": "未展示可验证结果",
    "最终状态错误": "任务结果错误",
    "操作对象错误": "任务结果错误",
    "操作参数错误": "任务结果错误",
    "操作路径错误": "任务结果错误",
    "条件判断错误": "任务结果错误",
    "条件分支错误": "任务结果错误",
    "严重意图偏离": "任务结果错误",
    "产生不当副作用": "任务结果错误",
    "自述与界面冲突": "回复与界面不一致",
    "界面与回复不一致": "回复与界面不一致",
    "严重回复质量问题": "其他执行问题",
    "最终回复瑕疵": "其他轻微问题",
    "待账号登陆": "缺少前置条件",
    "待账号登录": "缺少前置条件",
    "待权限授权": "缺少前置条件",
    "待信息补充": "待用户澄清",
    "待身份验证": "缺少前置条件",
    "待用户敏感操作": "缺少前置条件",
    "应用未安装": "缺少前置条件",
    "目标对象不存在": "缺少前置条件",
    "必要硬件缺失": "缺少前置条件",
    "设备或系统不支持": "缺少前置条件",
    "外部服务不可用": "缺少前置条件",
    "不具备完成条件": "缺少前置条件",
    "录屏证据缺失": "录屏数据不完整",
    "无验证结果": "未展示可验证结果",
    "证据冲突": "评测证据冲突",
    "录屏Query与输入Query不一致": QUERY_ALIGNMENT_ISSUE,
    "录屏Query未完整展示": QUERY_ALIGNMENT_ISSUE,
    "录屏未展示Query": QUERY_ALIGNMENT_ISSUE,
    "未归因": "其他执行问题",
    "其他不可评估原因": "其他未归类情况",
}
_FALLBACK_ISSUE = {
    "ok": "其他轻微问题",
    "nok": "其他执行问题",
    "no_support": "其他阻塞原因",
    "others": "其他未归类情况",
}

_MISNESTED_TOP_LEVEL_FIELDS = (
    "task_type",
    "total",
    "correctness",
    "issue_types",
    "need_review_cate",
    "cards",
    "error_type",
    "is_low_level",
    "rationale",
    "confidence",
    "execution_routes",
    "route_evidence",
    "route_rationale",
    "route_status",
)


def hoist_misnested_operation_fields(data: dict[str, Any]) -> dict[str, Any]:
    """容错模型把顶层任务类字段误放进 rubric 的常见单层括号错误。"""
    rubric = data.get("rubric")
    if not isinstance(rubric, dict):
        return data
    nested = [name for name in _MISNESTED_TOP_LEVEL_FIELDS if name in rubric]
    if not nested:
        return data
    normalized = dict(data)
    normalized_rubric = dict(rubric)
    for name in nested:
        if name not in normalized:
            normalized[name] = normalized_rubric[name]
        normalized_rubric.pop(name, None)
    normalized["rubric"] = normalized_rubric
    return normalized


def _as_issue_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for raw in values:
        if raw is None:
            continue
        for part in re.split(r"[,，;；、|/]+", str(raw)):
            issue = part.strip()
            if not issue or issue.lower() in {"null", "none", "n/a"}:
                continue
            normalized = _ALIASES.get(issue, issue)
            if normalized not in result:
                result.append(normalized)
    return result


def split_operation_issue_fields(
    issues: list[str], review_category_names: list[str] | set[str]
) -> tuple[list[str], list[str]]:
    """把执行质量问题与外部阻塞、证据或输入复核原因分开。"""
    review_names = set(review_category_names)
    return (
        [issue for issue in issues if issue not in review_names],
        [issue for issue in issues if issue in review_names],
    )


def collect_operation_output_issues(data: dict[str, Any]) -> list[str]:
    """兼容模型把旧 review 类型放在 issue_types 的输出。"""
    return list(dict.fromkeys([
        *_as_issue_list(data.get("issue_types", data.get("error_type"))),
        *_as_issue_list(data.get("need_review_cate")),
    ]))


def clean_operation_card_name(value: Any) -> str:
    """自定义类型只能是短类别名；内容、括号备注和解释不得混入类型列。"""
    name = re.split(r"[（(：:；;，,。\n]", str(value or "").strip(), maxsplit=1)[0].strip()
    return name if 1 < len(name) <= 12 else "其他卡"


def normalize_operation_cards(value: Any, known_types: dict[str, str]) -> list[OperationCard]:
    """未知卡片使用 other，保留观察名称，不凭模型猜测扩充枚举。"""
    cards: list[OperationCard] = []
    display_keys = {
        alias: key for key, name in known_types.items()
        for alias in (name, name.removesuffix("卡"))
    }
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        raw_type = str(raw.get("type") or "other").strip()
        raw_type = display_keys.get(raw_type, raw_type)
        observed_name = str(raw.get("observed_name") or "").strip()
        if raw_type not in known_types:
            raw_type = display_keys.get(clean_operation_card_name(observed_name), raw_type)
        if raw_type not in known_types and raw_type != "other":
            observed_name = clean_operation_card_name(observed_name or raw_type)
            raw_type = "other"
        elif raw_type in known_types:
            observed_name = known_types[raw_type]
        else:
            observed_name = clean_operation_card_name(observed_name)
        relation = raw.get("relation_to_query")
        suitability = raw.get("suitability")
        raw_frames = raw.get("evidence_frames") or []
        frames = []
        for frame in raw_frames if isinstance(raw_frames, list) else []:
            try:
                number = int(frame)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in frames:
                frames.append(number)
        cards.append(OperationCard(
            type=raw_type,
            observed_name=observed_name,
            visible_content=str(raw.get("visible_content") or "").strip(),
            target=str(raw.get("target") or "").strip(),
            relation_to_query=relation if relation in {"direct", "supporting", "unrelated", "unclear"} else "unclear",
            suitability=suitability if suitability in {"suitable", "unsuitable", "unclear"} else "unclear",
            reason=str(raw.get("reason") or "").strip(),
            evidence_frames=frames,
        ))
    return cards


def _normalize_route(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _ROUTE_ALIASES.get(text.lower()) or _ROUTE_ALIASES.get(text)


def _route_from_evidence(route: str, evidence: str) -> str:
    """对已明确约定的弱视觉特征做确定性门控，避免 Prompt 漂移。"""
    lowered = evidence.lower()
    if (
        route == "skill"
        and any(cue in evidence for cue in _GENERIC_TOOL_RETURN_CUES)
        and not any(cue in lowered for cue in _STRONG_SKILL_EVIDENCE_CUES)
    ):
        return "other"
    return route


def normalize_operation_routes(
    routes_value: Any,
    evidence_value: Any,
    status_value: Any,
) -> tuple[list[str], list[OperationRouteEvidence], str]:
    """归一化任务类多标签链路，保序去重并容忍中英文标签。"""
    raw_routes = routes_value if isinstance(routes_value, list) else []
    if isinstance(routes_value, str):
        raw_routes = re.split(r"[,，;；、|/]+", routes_value)

    routes: list[str] = []
    for raw in raw_routes:
        route = _normalize_route(raw)
        if route and route not in routes:
            routes.append(route)

    evidence_items = evidence_value if isinstance(evidence_value, list) else []
    evidence: list[OperationRouteEvidence] = []
    evidence_by_route: set[str] = set()
    remapped_routes: dict[str, str] = {}
    for raw in evidence_items:
        if not isinstance(raw, dict):
            continue
        source_route = _normalize_route(raw.get("route"))
        if not source_route:
            continue
        evidence_text = str(raw.get("evidence") or "").strip()
        route = _route_from_evidence(source_route, evidence_text)
        if route != source_route:
            remapped_routes[source_route] = route
        if route in evidence_by_route:
            continue
        frames: list[int] = []
        raw_frames = raw.get("evidence_frames")
        if isinstance(raw_frames, list):
            for frame in raw_frames:
                try:
                    number = int(frame)
                except (TypeError, ValueError):
                    continue
                if number > 0 and number not in frames:
                    frames.append(number)
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence.append(
            OperationRouteEvidence(
                route=route,
                evidence_frames=frames,
                evidence=evidence_text,
                confidence=confidence,
            )
        )
        evidence_by_route.add(route)
        if route not in routes:
            routes.append(route)

    for source_route, target_route in remapped_routes.items():
        if source_route not in evidence_by_route and source_route in routes:
            position = routes.index(source_route)
            routes.pop(position)
            if target_route not in routes:
                routes.insert(position, target_route)

    if "skill" in routes and "jarvis" in routes:
        skill_item = next((item for item in evidence if item.route == "skill"), None)
        if (
            skill_item
            and any(cue in skill_item.evidence for cue in _JARVIS_EMBEDDED_STEP_CUES)
            and not any(cue in skill_item.evidence for cue in _INDEPENDENT_SKILL_CUES)
        ):
            routes.remove("skill")
            evidence = [item for item in evidence if item.route != "skill"]

    if "other" in routes and any(route in _OPERATION_ROUTES for route in routes):
        other_item = next((item for item in evidence if item.route == "other"), None)
        other_evidence = other_item.evidence.lower() if other_item else ""
        if not any(cue in other_evidence for cue in _INDEPENDENT_OTHER_CUES):
            routes.remove("other")
            evidence = [item for item in evidence if item.route != "other"]

    evidence.sort(key=lambda item: routes.index(item.route))
    status = str(status_value or "").strip().lower()
    if status not in _VALID_ROUTE_STATUS:
        status = "detected" if routes else "insufficient_evidence"
    if routes and status == "insufficient_evidence":
        status = (
            "detected"
            if evidence and max(item.confidence for item in evidence) >= 0.6
            else "uncertain"
        )
    if not routes and status == "detected":
        status = "insufficient_evidence"
    return routes, evidence, status


def format_operation_route_rationale(
    routes: list[str], rationale_value: Any = None
) -> str:
    """输出与归一化链路一致的简短摘要，避免保留已被门控删除的标签。"""
    if routes:
        return "按首次出现顺序识别为：" + "；".join(
            _ROUTE_DISPLAY.get(route, route) for route in routes
        )
    return str(rationale_value or "").strip()


def normalize_operation_correctness(
    correctness: Any,
    issue_types: Any = None,
) -> OperationCorrectness:
    """归一新旧任务类判定；旧 unclear 按原错因区分客观阻塞与不可评估。"""
    raw = str(correctness or "").strip().lower()
    if raw in _VALID_CORRECTNESS:
        return raw  # type: ignore[return-value]
    if raw in _LEGACY_CORRECTNESS:
        return _LEGACY_CORRECTNESS[raw]  # type: ignore[return-value]
    if raw == "unclear":
        text = " ".join(_as_issue_list(issue_types))
        return "no_support" if any(hint in text for hint in _NO_SUPPORT_HINTS) else "others"
    return "others"


def _issue_rule_allowed(rule: Any) -> list[str]:
    if hasattr(rule, "allowed_correctness"):
        return list(rule.allowed_correctness)
    if isinstance(rule, dict):
        values = rule.get("allowed_correctness", [])
        return list(values) if isinstance(values, list) else []
    return []


def operation_issue_catalog(
    issue_type_rules: dict[str, Any] | None,
) -> dict[str, set[str]]:
    """将新目录或旧按 correctness 分组配置统一为 issue -> 允许状态。"""
    if not issue_type_rules:
        return {}
    statuses = {"ok", "nok", "no_support", "others"}
    if set(issue_type_rules) == statuses and all(
        isinstance(values, list) for values in issue_type_rules.values()
    ):
        catalog: dict[str, set[str]] = {}
        for status, values in issue_type_rules.items():
            for issue in values:
                catalog.setdefault(str(issue), set()).add(status)
        return catalog
    return {
        name: set(_issue_rule_allowed(rule))
        for name, rule in issue_type_rules.items()
        if _issue_rule_allowed(rule)
    }


def primary_operation_issue_types(
    correctness: OperationCorrectness,
    issue_type_rules: dict[str, Any] | None,
) -> set[str]:
    """返回可作为整体 correctness 主因的类型；通用质量问题不能作非 ok 主因。"""
    catalog = operation_issue_catalog(issue_type_rules)
    all_statuses = {"ok", "nok", "no_support", "others"}
    if correctness == "ok":
        return {name for name, allowed in catalog.items() if "ok" in allowed}
    if correctness == "nok":
        return {name for name, allowed in catalog.items() if allowed == {"nok"}}
    return {
        name
        for name, allowed in catalog.items()
        if correctness in allowed and allowed != all_statuses
    }


def normalize_operation_fields(
    correctness: Any,
    issue_types: Any,
    is_low_level: Any,
    task_type: str | None = None,
    allowed_issue_types: dict[str, Any] | None = None,
) -> tuple[OperationCorrectness, list[str], str]:
    """规范 correctness、中文问题数组和低级错误标识。"""
    normalized_correctness = normalize_operation_correctness(correctness, issue_types)
    issues = _as_issue_list(issue_types)

    # Query 对齐是评测输入门禁，而非 agent 执行问题。命中后不再混入
    # 其他执行归因，确保下游可以稳定筛出配对异常样本。
    if QUERY_ALIGNMENT_ISSUE in issues:
        return "others", [QUERY_ALIGNMENT_ISSUE], "no"

    if FACT_REVIEW_CATEGORY in issues:
        catalog = operation_issue_catalog(allowed_issue_types)
        independent_error = any(catalog.get(issue) == {"nok"} for issue in issues)
        normalized_correctness = "nok" if independent_error else "others"

    # 新标准：仅缺少结果证据属于评测侧无法确认，主判 others；如果同一复杂
    # 任务还存在独立的可归责错误，则保留 nok，并把证据不足作为次要问题。
    if normalized_correctness == "nok" and any(
        issue in issues for issue in ("未展示可验证结果", FACT_REVIEW_CATEGORY)
    ):
        if allowed_issue_types:
            issue_catalog = operation_issue_catalog(allowed_issue_types)
            has_independent_nok = any(
                issue_catalog.get(issue) == {"nok"} for issue in issues
            )
        else:
            has_independent_nok = any(
                issue not in {"未展示可验证结果", FACT_REVIEW_CATEGORY} for issue in issues
            )
        if not has_independent_nok:
            normalized_correctness = "others"

    if allowed_issue_types:
        catalog = operation_issue_catalog(allowed_issue_types)
        unknown = any(issue not in catalog for issue in issues)
        issues = [
            issue
            for issue in issues
            if normalized_correctness in catalog.get(issue, set())
        ]
        primary_allowed = primary_operation_issue_types(
            normalized_correctness,
            allowed_issue_types,
        )
        primary = next((issue for issue in issues if issue in primary_allowed), None)
        if unknown and _FALLBACK_ISSUE[normalized_correctness] not in issues:
            issues.append(_FALLBACK_ISSUE[normalized_correctness])
        if normalized_correctness != "ok" and primary is None:
            issues.insert(0, _FALLBACK_ISSUE[normalized_correctness])
        elif primary is not None and issues[0] != primary:
            issues.remove(primary)
            issues.insert(0, primary)

    if normalized_correctness != "ok" and not issues:
        issues = [_FALLBACK_ISSUE[normalized_correctness]]

    low_level = "no"
    if normalized_correctness == "nok" and str(task_type or "").strip().lower() != "complex":
        has_blocker = any(
            any(hint in issue for hint in _NO_SUPPORT_HINTS)
            for issue in issues
        )
        if not has_blocker:
            if isinstance(is_low_level, bool):
                low_level = "yes" if is_low_level else "no"
            else:
                raw = str(is_low_level or "").strip().lower()
                low_level = "yes" if raw in {"yes", "true", "1", "是"} else "no"

    return normalized_correctness, list(dict.fromkeys(issues)), low_level


def map_legacy_operation_result(
    correctness: Any,
    error_type: Any,
) -> tuple[OperationCorrectness, list[str]]:
    """读取旧任务结果时转换字段，不修改历史文件。"""
    normalized, issues, _ = normalize_operation_fields(
        correctness,
        error_type,
        "no",
    )
    return normalized, issues
