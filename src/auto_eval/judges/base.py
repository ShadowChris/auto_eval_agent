"""裁判客户端：一个可多轮调用外部工具的评测智能体（agent loop）。

模仿人类反复查证后再评判：裁判在 loop 中自主决定查什么、何时停止，
可调用 web_search（搜索）/ fetch_page（抓网页）等工具，直到对事实确信后输出最终评判。

可选明细日志：设环境变量 AUTO_EVAL_JUDGE_TRACE=<jsonl路径> 后，每次 complete 调用会
把每轮 LLM 响应、每次工具的完整返回、最终对话历史追加到该文件（默认关，不产生开销）。

流式输出：complete() 支持 stream_callback，每收到 token 时回调，用于前端实时展示裁判
思考过程。仅负责推送文本，不改变 agent loop 的控制流（tool_call 仍依赖完整响应）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import JudgeConfig
from ..llm_stream import build_openai_client, stream_chat_completion
from ..observability import bind_chain_context, log_event
from ..paths import resolve_project_path
from .prompts import persona_text
from .tools import build_tools

logger = logging.getLogger("auto_eval.judge")


@dataclass
class JudgeReply:
    content: str
    used_search: bool = False
    search_queries: list[str] = field(default_factory=list)
    tool_trace: list[str] = field(default_factory=list)  # 摘要级轨迹（给报告/结果表用）
    rounds: int = 0  # agent loop 实际轮数
    truncated: bool = False  # 是否因达到 max_rounds 被截断（已用强制判定兜底）


class JudgeOutputParseError(ValueError):
    """裁判调用成功，但最终结构化输出在定向修复后仍无法解析。"""

    def __init__(
        self,
        message: str,
        *,
        raw_output: str,
        repair_output: str,
        judge: str,
        model: str,
    ):
        super().__init__(message)
        self.raw_output = raw_output
        self.repair_output = repair_output
        self.judge = judge
        self.model = model

def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
    }


def _safe_json(s: str | None):
    """把模型返回的 tool_call arguments 字符串解析成 dict（消除 \\uXXXX 转义，便于阅读）。失败回退原文。"""
    try:
        return json.loads(s or "{}")
    except Exception:
        return s


def _redact_image_urls(messages: list[dict], refs: list[str] | None) -> list[dict]:
    """把 messages 里 image_url 的 base64 data url 替换成帧路径引用，避免 trace 文件膨胀。

    操作类评测每帧 base64 约 30KB，N 帧会让 judge_calls.jsonl 单行膨胀到 MB 级。
    refs 与 complete 的 user_images 一一对应（通常是关键帧本地路径）；
    无 refs 对应时标记 data url 已省略。仅影响 trace 落盘，不影响发给模型的消息。
    """
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            new_content, img_idx = [], 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    ref = refs[img_idx] if refs and img_idx < len(refs) else "(base64 省略)"
                    new_content.append({"type": "image_url", "image_url": {"url": f"[frame → {ref}]"}})
                    img_idx += 1
                else:
                    new_content.append(part)
            out.append({**m, "content": new_content})
        else:
            out.append(m)
    return out


class JudgeClient:
    def __init__(
        self,
        cfg: JudgeConfig,
        search_providers=None,
        search_topk: int = 3,
        max_rounds: int = 12,
        trace_path: str | None = None,
    ):
        if not cfg.base_url:
            raise ValueError(f"裁判[{cfg.name}] 缺少 base_url")
        self.cfg = cfg
        self.client = build_openai_client(
            base_url=cfg.base_url,
            api_key=cfg.api_key() or "EMPTY",
            connect_timeout_s=cfg.connect_timeout_s,
            read_timeout_s=cfg.read_timeout_s,
        )
        self.model = cfg.model or cfg.name
        self.persona = persona_text(cfg.persona)
        self.max_rounds = max_rounds
        # 明细日志路径：优先构造参数，其次环境变量；都不给则不记录
        _trace_path = trace_path or os.environ.get("AUTO_EVAL_JUDGE_TRACE")
        self.trace_path = str(resolve_project_path(_trace_path)) if _trace_path else None
        self.tool_defs, self.tool_map = build_tools(
            web_search_enabled=cfg.enable_web_search,
            search_providers=search_providers,
            search_topk=search_topk,
            fetch_enabled=getattr(cfg, "enable_fetch", True),
            calculate_enabled=getattr(cfg, "enable_calculate", True),
            python_enabled=getattr(cfg, "enable_python", False),
        )
        self.has_tools = bool(self.tool_defs)

    async def repair_json(
        self,
        malformed_output: str,
        *,
        label: str = "裁判输出",
        round_no: int = 0,
    ) -> str:
        """只修复最终 JSON 语法，不重新执行分类、检索或整条 Agent Loop。"""
        judge_label = f"{self.cfg.display or self.cfg.name}({self.cfg.name})"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 JSON 格式修复器。只修复输入中的 JSON 语法和括号结构，"
                    "必须保留原有字段、分数、判定和理由语义，不得重新评审、不得增删事实。"
                    "只输出一个合法 JSON 对象，不要输出 Markdown、分析或解释。"
                ),
            },
            {
                "role": "user",
                "content": f"需要修复的{label}如下：\n\n{malformed_output}",
            },
        ]
        with bind_chain_context(
            module="模型裁判",
            judge=judge_label,
            round=max(1, round_no),
        ):
            log_event(
                "模型裁判",
                "JSON格式修复开始",
                level=logging.WARNING,
                details={"原始输出字符": len(malformed_output)},
                progress=82,
                progress_message=f"{judge_label} · 修复JSON格式",
            )
            response = await self._llm_create(
                {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0,
                }
            )
            content = response.choices[0].message.content or ""
            log_event(
                "模型裁判",
                "JSON格式修复完成",
                details={"修复输出字符": len(content)},
                progress=85,
                progress_message=f"{judge_label} · JSON格式修复完成",
            )
        return content

    async def complete(self, system: str, user: str,
                       stream_callback: Callable[[str], None] | None = None,
                       user_images: list[str] | None = None,
                       user_image_refs: list[str] | None = None) -> JudgeReply:
        # 多模态：操作类评测传入关键帧 data_url 时，user content 变成 [text, image_url...] 列表。
        # agent-loop 内追加的 assistant/tool/强制判定消息仍为字符串，不受影响。
        user_content: Any = user
        if user_images:
            user_content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in user_images
            ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        trace: list[str] = []
        queries: list[str] = []
        used_search = False
        last_content = ""
        rounds = 0
        do_trace = bool(self.trace_path)
        llm_rounds: list[dict] = [] if do_trace else []  # 仅 do_trace 时填充
        tool_results: list[dict] = [] if do_trace else []

        truncated = False
        for _ in range(self.max_rounds):
            rounds += 1
            judge_label = f"{self.cfg.display or self.cfg.name}({self.cfg.name})"
            with bind_chain_context(
                module="模型裁判", judge=judge_label, round=rounds
            ):
                kwargs = {"model": self.model, "messages": messages, "temperature": self.cfg.temperature}
                if self.has_tools:
                    kwargs["tools"] = self.tool_defs
                    kwargs["tool_choice"] = "auto"
                resp = await self._llm_create(kwargs, stream_callback=stream_callback)
            msg = resp.choices[0].message
            last_content = msg.content or ""
            tool_calls = getattr(msg, "tool_calls", None)

            if do_trace:
                llm_rounds.append({
                    "round": rounds,
                    "content": msg.content,
                    "tool_calls": [
                        {"name": tc.function.name, "arguments": _safe_json(tc.function.arguments)}
                        for tc in (tool_calls or [])
                    ],
                    "finish_reason": getattr(resp.choices[0], "finish_reason", None),
                    "usage": _usage_dict(getattr(resp, "usage", None)),
                })

            if not tool_calls:
                break  # 裁判不再调工具 → 已确信，给出最终评判

            # 把带 tool_calls 的 assistant 消息加回去
            assistant_msg = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)
            for tc in tool_calls:
                name = tc.function.name
                args = _safe_json(tc.function.arguments)
                if not isinstance(args, dict):
                    result = f"(工具参数不是合法 JSON 对象: {args})"
                    summary = f"{name}(参数格式错误)"
                    with bind_chain_context(
                        module="工具调用", judge=judge_label, round=rounds
                    ):
                        log_event(
                            "工具调用",
                            "参数错误",
                            level=logging.ERROR,
                            details={"工具": name, "参数": args},
                            progress=70,
                            progress_message=f"{judge_label} · 第{rounds}轮 · 工具参数错误",
                            progress_status="error",
                        )
                    trace.append(summary)
                    if do_trace:
                        tool_results.append({"name": name, "args": args, "result": result})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    continue
                with bind_chain_context(
                    module="工具调用", judge=judge_label, round=rounds
                ):
                    log_event(
                        "工具调用",
                        "开始",
                        details={"工具": name, "参数": args},
                        progress=65,
                        progress_message=f"{judge_label} · 第{rounds}轮 · 调用{name}",
                    )
                    tool_started = time.perf_counter()
                    result, summary = await self._exec_tool_async(name, args)
                    tool_failed = (
                        str(result).startswith("(工具")
                        or "超时" in summary
                        or "错误" in summary
                    )
                    log_event(
                        "工具调用",
                        "失败" if tool_failed else "成功",
                        level=logging.ERROR if tool_failed else logging.INFO,
                        details={
                            "工具": name,
                            "结果": summary,
                            "耗时": f"{time.perf_counter() - tool_started:.2f}秒",
                        },
                        progress=75,
                        progress_message=(
                            f"{judge_label} · 第{rounds}轮 · {name}调用失败"
                            if tool_failed
                            else f"{judge_label} · 第{rounds}轮 · {name}调用成功"
                        ),
                        progress_status="running",
                    )
                trace.append(summary)
                if do_trace:
                    tool_results.append({"name": name, "args": args, "result": result})
                if name == "web_search" and args.get("query"):
                    queries.append(args["query"])
                    if not result.startswith("(无检索结果"):
                        used_search = True
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            # 循环自然结束（未 break）= 达到 max_rounds 仍想调工具 = 被截断
            truncated = True

        # 截断强制判定：追加一次「无工具」调用，让裁判基于已收集信息直接出最终 JSON，
        # 避免复杂题因查证上瘾导致完全没有评分输出。
        if truncated:
            messages.append({
                "role": "user",
                "content": "你已收集足够信息（或已达工具调用上限）。请不要再调用任何工具，"
                           "直接输出最终的 <analysis>...</analysis> 思考与 JSON 判定。",
            })
            judge_label = f"{self.cfg.display or self.cfg.name}({self.cfg.name})"
            with bind_chain_context(
                module="模型裁判", judge=judge_label, round=rounds + 1
            ):
                log_event(
                    "模型裁判",
                    "达到轮次上限，强制生成判定",
                    level=logging.WARNING,
                    details={"最大轮次": self.max_rounds},
                    progress=80,
                    progress_message=f"{judge_label} · 强制生成最终判定",
                )
                resp = await self._llm_create(
                    {"model": self.model, "messages": messages, "temperature": self.cfg.temperature},
                    stream_callback=stream_callback,
                )
            msg = resp.choices[0].message
            last_content = msg.content or ""
            rounds += 1
            if do_trace:
                llm_rounds.append({
                    "round": rounds, "content": last_content, "tool_calls": [],
                    "finish_reason": "force_judgement", "usage": _usage_dict(getattr(resp, "usage", None)),
                })

        if do_trace:
            self._write_trace({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "judge": self.cfg.name,
                "model": self.model,
                "system": system,
                "user": user,
                "rounds": rounds,
                "used_search": used_search,
                "search_queries": queries,
                "truncated": truncated,
                "llm_rounds": llm_rounds,
                "tool_results": tool_results,
                # trace 不存 base64（每帧 ~30KB×N 会让 jsonl 膨胀），image_url 换成帧路径引用
                "image_refs": user_image_refs,
                "messages": _redact_image_urls(messages, user_image_refs),
            })

        return JudgeReply(
            content=last_content,
            used_search=used_search,
            search_queries=queries,
            tool_trace=trace,
            rounds=rounds,
            truncated=truncated,
        )

    def _write_trace(self, detail: dict[str, Any]) -> None:
        assert self.trace_path
        try:
            d = os.path.dirname(self.trace_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(detail, ensure_ascii=False) + "\n")
        except Exception:
            # 日志失败不应影响评测主流程
            pass

    async def _llm_create(self, kwargs: dict, max_attempts: int | None = None,
                          stream_callback: Callable[[str], None] | None = None):
        """始终使用流式接口；callback 只负责可选的前端分片通知。"""
        return await self._llm_create_stream(
            kwargs,
            callback=stream_callback,
            max_attempts=max_attempts,
        )

    async def _llm_create_stream(
        self,
        kwargs: dict,
        callback: Callable[[str], None] | None = None,
        max_attempts: int | None = None,
    ):
        """流式调用 LLM，逐 token 回调，同时累积完整响应供 tool_call 解析。"""
        return await stream_chat_completion(
            self.client,
            kwargs,
            callback=callback,
            include_usage=self.cfg.stream_include_usage,
            total_timeout_s=self.cfg.total_timeout_s,
            max_attempts=max_attempts or self.cfg.max_attempts,
            retry_base_s=self.cfg.retry_base_s,
            retry_max_s=self.cfg.retry_max_s,
        )

    async def _exec_tool_async(self, name: str, args: dict) -> tuple[str, str]:
        """在线程中执行同步工具，避免 web_search/fetch_page 阻塞事件循环。"""
        timeout = float(os.environ.get("AUTO_EVAL_TOOL_TIMEOUT", "12"))
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._exec_tool, name, args), timeout=timeout)
        except asyncio.TimeoutError:
            return f"(工具超时: {name} 超过 {timeout:.0f} 秒)", f"{name}({args})=超时"
    def _exec_tool(self, name: str, args: dict) -> tuple[str, str]:
        fn = self.tool_map.get(name)
        if not fn:
            return "(未知工具)", f"{name}(?)=未知"
        try:
            out = fn(**args) if isinstance(args, dict) else fn(args)
        except Exception as e:
            return f"(工具出错: {e})", f"{name}({args})=错误"
        if isinstance(out, list):
            text = "\n".join(out) if out else "(无检索结果，请基于自身知识判断)"
            summary = f"search[{args.get('query','')}]→{len(out)}条"
        else:
            text = out or "(无内容)"
            if name == "fetch_page":
                summary = f"fetch[{str(args.get('url',''))[:60]}]→{len(text)}字"
            elif name == "calculate":
                summary = f"calc[{args.get('expression','')}]→{text[:40]}"
            elif name == "python_run":
                summary = f"py[{len(str(args.get('code','')))}字符]→{text[:40]}"
            else:
                summary = f"{name}→{text[:40]}"
        return text, summary
