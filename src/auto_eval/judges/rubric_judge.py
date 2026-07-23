"""Rubric 盲打分裁判：意图理解 + 理想锚定 + 多角度分析的深度盲评（可联网）。"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from pathlib import Path

from ..config import RubricDim
from ..llm_stream import build_openai_client, stream_chat_completion
from ..media import encode_frame
from ..observability import bind_chain_context, log_event
from ..schema import EvalItem, SingleScore

logger = logging.getLogger("auto_eval.classify")
from .base import JudgeClient, JudgeOutputParseError
from .prompts import (
    OPERATION_SYSTEM,
    OPERATION_USER,
    RUBRIC_COMPARE_SYSTEM,
    RUBRIC_COMPARE_USER,
    RUBRIC_PROCESS_SYSTEM,
    RUBRIC_PROCESS_USER,
    RUBRIC_SYSTEM,
    RUBRIC_USER,
    parse_analysis,
    parse_json_loose,
    resolve_prompt_context,
)

_VALID = {"right", "wrong", "partial", "unclear"}


def _flatten_rubric(raw, dim_names=None):
    """将嵌套（一级→二级）rubric 展平为一级分 dict，并提取每个一级维度的打分理由。
    兼容旧格式（直接分 / 无 reason）。支持 null 表示 N/A（不适用）。
    返回 (rubric, reasons, na_dimensions)。"""
    out = {}
    reasons: dict[str, str] = {}
    na_dims: list[str] = []
    for k, v in (raw or {}).items():
        if v is None:
            # 整个一级维度标为 N/A
            na_dims.append(k)
            continue
        if isinstance(v, dict):
            # 过滤掉值为 null 的子维度（不参与均值计算）
            nums = []
            for sk, sv in v.items():
                if sk in ("total", "reason"):
                    continue
                if sv is None:
                    continue  # 子维度 N/A，跳过
                if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                    nums.append(sv)
            if nums:
                # 有至少一个适用子维度 → 取均值作为该一级维度分
                if "total" in v and isinstance(v["total"], (int, float)) and not isinstance(v["total"], bool):
                    out[k] = int(v["total"])
                else:
                    out[k] = round(sum(nums) / len(nums))
            elif isinstance(v.get("total"), (int, float)) and not isinstance(v.get("total"), bool):
                # 无二级维度的统一格式：{"total": score, "reason": "..."}
                out[k] = int(v["total"])
            else:
                # 没有可用 total，或所有子维度都 N/A → 整个一级维度 N/A
                na_dims.append(k)
                if v.get("reason"):
                    reasons[k] = str(v["reason"])
                continue
            if v.get("reason"):
                reasons[k] = str(v["reason"])
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
    if dim_names and out and set(out.keys()) != set(dim_names):
        vals = list(out.values())
        if len(vals) == len(dim_names):
            out = {dim_names[i]: vals[i] for i in range(len(dim_names))}
    return out, reasons, na_dims


async def ensure_classified(item: EvalItem, skill_router, *,
                            model: str, base_url: str, api_key: str,
                            timeout: float = 15.0) -> None:
    """每个 case 仅调用一次：用轻量模型做垂域分类，结果缓存到 item.category。
    后续所有裁判共享此结果，避免重复分类。
    分类失败不阻断评测 → 记录日志，回落 default。
    """
    if item.category and item.category != "default":
        return  # 已分类（dataset 预标 或 已跑过）
    client = None
    try:
        client = build_openai_client(
            base_url=base_url,
            api_key=api_key,
            connect_timeout_s=min(10.0, timeout),
            read_timeout_s=timeout,
        )
        result = await _classify(item, client, model, skill_router)
        if result:
            item.category = result
            item.metadata["category_source"] = "auto_classified"
        else:
            item.metadata.setdefault("category_source", "fallback_default")
    except Exception:
        logger.exception("classify failed, falling back to default: id=%s", item.id)
        item.metadata.setdefault("category_source", "fallback_default")
    finally:
        if client is not None:
            await client.close()


class RubricJudge:
    def __init__(
        self,
        client: JudgeClient,
        dims: list[RubricDim],
        skill_router=None,
        evaluation_time: datetime | None = None,
    ):
        self.client = client
        self.dims = dims
        self.scale = dims[0].scale if dims else 5
        self.skill_router = skill_router
        self.evaluation_time = evaluation_time

    async def score(self, item: EvalItem, model_name: str, answer: str, run_idx: int = 0,
                    eval_mode: str = "result", process_dims=None, competitor: str | None = None,
                    stream_callback=None) -> SingleScore:
        # 操控类只使用样本显式提供的背景，避免把评测时间误当成录屏执行时间。
        # 其他模式仍保留当前时间兜底，用于时效性事实判断。
        prompt_context = (
            (item.context or "").strip()
            if eval_mode == "operation"
            else resolve_prompt_context(item.context, self.evaluation_time)
        )
        # 自动垂域分类：若未预标 → 兜底用当前裁判 model 分类（正常流程已在 ensure_classified 完成）
        if (
            (not item.category or item.category == "default")
            and self.skill_router
            and not item.metadata.get("category_source")
        ):
            label = await _classify(item, self.client.client, self.client.model, self.skill_router)
            if label:
                item.category = label
                item.metadata["category_source"] = "auto_classified"
            else:
                item.metadata.setdefault("category_source", "fallback_default")
        else:
            item.metadata.setdefault("category_source", "dataset")
        skill_dims, skill_rules, _ = (self.skill_router.match(item) if self.skill_router else (None, "", []))
        is_product_compare = (self.client.cfg.persona == "product_expert") and bool(competitor)
        user_images: list[str] | None = None  # 操作类评测：关键帧 data_url 列表，其余模式为 None
        user_image_refs: list[str] | None = None  # 关键帧本地路径（仅写入 trace 供回溯，不展示前端）
        if eval_mode == "operation":
            op_skill = self.skill_router.domain.get("operation") if self.skill_router else None
            dims = (op_skill.rubrics if op_skill and op_skill.rubrics else None) or self.dims
            system = OPERATION_SYSTEM.render(
                persona=self.client.persona,
                agent_claim=(answer or "").strip(),
                dims=dims,
                scale=dims[0].scale if dims else 5,
            )
            user = OPERATION_USER.render(
                question=item.question, context=prompt_context
            )
            frames = item.metadata.get("frames") or []
            user_images = [encode_frame(Path(p)) for p in frames] if frames else None
            user_image_refs = frames if frames else None
        elif eval_mode == "process" and process_dims and item.trace:
            dims = process_dims  # 过程盲评维度不变（不受垂域 skill 影响）
            system = RUBRIC_PROCESS_SYSTEM.render(
                persona=self.client.persona, scale=dims[0].scale if dims else 5, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_PROCESS_USER.render(
                question=item.question, context=prompt_context, answer=answer, trace=item.trace
            )
        elif is_product_compare:
            dims = skill_dims or self.dims  # 产品专家：待评 vs 竞品 对比盲评
            system = RUBRIC_COMPARE_SYSTEM.render(
                persona=self.client.persona, scale=self.scale, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_COMPARE_USER.render(
                question=item.question, context=prompt_context, model_name=model_name, answer=answer,
                competitor=competitor
            )
        else:
            dims = skill_dims or self.dims  # 垂域 skill 维度优先
            system = RUBRIC_SYSTEM.render(
                persona=self.client.persona, scale=self.scale, dims=dims, skill_rules=skill_rules
            )
            user = RUBRIC_USER.render(
                question=item.question, context=prompt_context, model_name=model_name, answer=answer
            )
        t0 = time.perf_counter()
        if stream_callback is None and user_images is None and user_image_refs is None:
            reply = await self.client.complete(system, user)
        else:
            reply = await self.client.complete(
                system,
                user,
                stream_callback=stream_callback,
                user_images=user_images,
                user_image_refs=user_image_refs,
            )
        latency = int((time.perf_counter() - t0) * 1000)

        analysis = parse_analysis(reply.content)
        data = parse_json_loose(reply.content)
        if data is None:
            repaired = await self.client.repair_json(
                reply.content,
                label="裁判输出",
                round_no=reply.rounds + 1,
            )
            data = parse_json_loose(repaired)
            if data is None:
                raise JudgeOutputParseError(
                    "裁判输出定向修复后仍无法解析为 JSON",
                    raw_output=reply.content,
                    repair_output=repaired,
                    judge=self.client.cfg.name,
                    model=self.client.model,
                )
        rubric_raw = data.get("rubric") or {}
        rubric, rubric_reasons, na_dimensions = _flatten_rubric(rubric_raw, dim_names=[d.name for d in dims])
        if data.get("total") is not None:
            total = float(data["total"])
        else:
            total = sum(rubric.values()) / len(rubric) if rubric else 0.0
        correctness = data.get("correctness", "unclear")
        if correctness not in _VALID:
            correctness = "unclear"
        error_type = data.get("error_type")
        rationale = data.get("rationale", "")

        return SingleScore(
            item_id=item.id,
            model=model_name,
            judge=self.client.cfg.name,
            persona=self.client.cfg.persona,
            run_idx=run_idx,
            rubric=rubric,
            rubric_reasons=rubric_reasons,
            na_dimensions=na_dimensions,
            total=total,
            correctness=correctness,
            error_type=error_type,
            rationale=rationale,
            analysis=analysis,
            used_search=reply.used_search,
            tool_trace=reply.tool_trace,
            search_queries=reply.search_queries,
            truncated=reply.truncated,
            latency_ms=latency,
        )

def _skill_labels(skill_router) -> list[tuple[str, str]]:
    """候选 (name, 展示文字)：展示文字优先 Skill 的 display，否则回落 name；去掉 default。"""
    if not skill_router:
        return []
    return [
        (s.name, s.display or s.name)
        for s in skill_router.domain.values()
        if s.name and s.name != "default"
    ]


def _normalize_label(text: str, labels: list[tuple[str, str]], fallback: str) -> str | None:
    """把 LLM 分类输出归一化为 skill name。
    命中兜底出口(fallback)或无法识别 → None，让 category 保持 default、路由走 default 兜底；
    命中某 skill 的 name 或 display → 返回该 name（SkillRouter 按 name 匹配）。

    匹配优先级：精确匹配 > 最长子串匹配（避免短 display 误吞长 display）。
    """
    if not text:
        return None
    text = text.strip()
    if fallback and fallback in text:
        return None
    # 精确匹配（忽略大小写和首尾空白）
    for name, disp in labels:
        if text == name or text == disp:
            return name
    # 子串匹配：取最长的匹配（"电子数码" 优先于 "电子"）
    best, best_len = None, 0
    for name, disp in labels:
        if name in text and len(name) > best_len:
            best, best_len = name, len(name)
        if disp in text and len(disp) > best_len:
            best, best_len = name, len(disp)
    return best

# 垂域分类 few-shot 示例：key 是 skill name（稳定标识），value 是示例 query 列表。
# 渲染时用当前 display name 替换，确保改了 display 后示例自动跟随。
_FEWSHOT: dict[str, list[str]] = {
    "digital_3c": ["哪些手机支持卫星通信？", "某手机今天发布了什么配置？"],
    "search": ["帮我找华为手机参数官网链接"],
    "news": ["今天有哪些重要科技行业新闻？"],
    "lbs_travel": ["规划上海三日游路线"],
    "document": ["总结这份PDF的结论"],
    "music": ["这首歌属于什么风格？"],
    "film_tv": ["这部电影适合儿童看吗？"],
    "automotive": ["宝马X5和奔驰GLE哪个更值得买？"],
    "sports": ["2026年世界杯冠军是谁？"],
    "math_solving": ["解方程 x^2 + 2x - 8 = 0"],
}

async def _classify(item: EvalItem, client, model: str, skill_router=None) -> str | None:
    """轻量垂域分类：未标记 category 时，让裁判从各 Skill 的展示名里选一个；
    若不属于任何一类，输出 default 的展示名（如"通用"）以回落 default。
    候选动态来自 config/skills/*.yaml。只用一次 LLM 调用、不调工具；
    max_tokens=50 防止推理模型被 reasoning 吃光 token。"""
    labels = _skill_labels(skill_router)  # [(name, display_text), ...]
    if not labels:
        return None  # 未配置 Skill → 不分类，回落 default
    # default 作为"不属于任何一类"的兜底出口词（取其 display，缺省为"通用"）
    default_skill = skill_router.domain.get("default")
    fallback = default_skill.display if default_skill and default_skill.display else "通用"
    shown = " / ".join(d for _, d in labels)
    definitions = []
    # 建立 name → display 映射，few-shot 示例和分类原则用
    name_to_disp: dict[str, str] = {}
    for name, display in labels:
        skill = skill_router.domain.get(name)
        rule = (skill.rules or '').strip() if skill else ''
        definitions.append(f"- {display}：{rule or '按用户核心意图判断是否属于该类'}")
        name_to_disp[name] = display
    # 动态渲染 few-shot 示例：用当前 display 名替换硬编码标签
    fewshot_lines: list[str] = []
    for skill_name, queries in _FEWSHOT.items():
        disp = name_to_disp.get(skill_name)
        if not disp:
            continue  # 该 skill 不存在（可能被删除），跳过
        for q in queries:
            fewshot_lines.append(f'- "{q}" → {disp}')
    # 动态渲染分类原则：用当前 display 名
    disp_d3c = name_to_disp.get("digital_3c", "数码3C")
    disp_auto = name_to_disp.get("automotive", "汽车")
    disp_sports = name_to_disp.get("sports", "体育")
    disp_music = name_to_disp.get("music", "音乐")
    disp_film = name_to_disp.get("film_tv", "影视")
    disp_search = name_to_disp.get("search", "搜索")
    disp_news = name_to_disp.get("news", "新闻")
    disp_doc = name_to_disp.get("document", "文档")
    disp_lbs = name_to_disp.get("lbs_travel", "LBS（旅行规划）")
    disp_math = name_to_disp.get("math_solving", "数学解题")
    system = (
        "你是查询意图分类器。请理解用户真正希望得到的结果，而不是只匹配关键词。\n"
        f"只能从以下标签中选择一个：{shown} / {fallback}。\n\n"
        "类别说明：\n" + "\n".join(definitions) + "\n"
        f"- {fallback}：无法明确归入上述类别的通用问答。\n\n"
        "分类原则：\n"
        "1. 优先按用户问题的核心对象和最终交付物分类，不按单个关键词分类。\n"
        f"2. 垂直主题优先：手机/电脑归{disp_d3c}，车型归{disp_auto}，赛事归{disp_sports}，歌曲归{disp_music}，影视作品归{disp_film}。\n"
        f"3. {disp_news}只用于公共事件、时政、财经和社会热点；某款手机或汽车的发布参数仍归对应垂域。\n"
        f"4. {disp_search}只用于用户明确要求找网页、链接、资料、出处或资源；直接回答某垂域事实仍归对应垂域。\n"
        f"5. {disp_doc}用于基于给定文件内容的摘要、抽取、比较、改写或问答。\n"
        f"6. {disp_lbs}用于路线、行程、地点、酒店、景点、餐饮和导航规划。\n"
        f"7. 星期、日期、节假日等日历事实默认归{fallback}；只有明确要求日期计算、间隔计算、日历推导或展示计算过程时才归{disp_math}。\n"
        "8. 同时包含多个意图时，以用户最终希望你交付的核心结果分类。\n\n"
        "对比例子：\n" + "\n".join(fewshot_lines) + "\n\n"
        "在心中完成意图判断后，只输出标签本身，不输出解释、标点或 JSON。"
    )
    try:
        logger.info("classify start: id=%s query=%.120s", item.id, item.question)
        query_text = f"查询：{item.question}"
        if item.context:
            query_text += (
                "\n可信背景条件（由评测样本提供，请作为意图判断前提）："
                f"\n{item.context}"
            )
        with bind_chain_context(module="垂域分类", round=0):
            log_event(
                "垂域分类",
                "开始",
                details={"模型": model, "问题": item.question},
                progress=10,
                progress_message="正在进行垂域分类",
            )
            resp = await stream_chat_completion(
                client,
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"{query_text}\n\n标签："},
                    ],
                    "temperature": 0,
                    "max_tokens": 200,
                },
                total_timeout_s=15.0,
                max_attempts=3,
            )
        text = (resp.choices[0].message.content or "").strip()
        result = _normalize_label(text, labels, fallback)
        if result:
            logger.info("classify ok: id=%s raw=%r -> skill=%s", item.id, text, result)
        else:
            logger.info("classify fallback: id=%s raw=%r -> default", item.id, text)
        with bind_chain_context(module="垂域分类", round=0):
            log_event(
                "垂域分类",
                "成功",
                details={"模型输出": text, "分类": result or "通用"},
                progress=20,
                progress_message=f"垂域分类完成：{result or '通用'}",
            )
        return result
    except Exception as exc:
        logger.exception("classify failed: id=%s", item.id)
        with bind_chain_context(module="垂域分类", round=0):
            log_event(
                "垂域分类",
                "失败，回退通用分类",
                level=logging.ERROR,
                details={"错误类型": type(exc).__name__, "错误": str(exc)},
                progress=20,
                progress_message="垂域分类失败，已回退通用分类",
            )
        return None
