"""裁判客户端：按 SYSTEM/USER 模板单轮直出的评测调用。

裁判一次生成 <analysis> 思考链 + 结论 JSON；解析失败由上层走定向修复
（repair_json，只修 JSON 语法不重新评审）。流式输出：complete() 支持
stream_callback，每收到 token 时回调，用于前端实时展示裁判思考过程。

可选明细日志：设环境变量 AUTO_EVAL_JUDGE_TRACE=<jsonl路径> 后，每次 complete
调用会把 LLM 响应、对话历史追加到该文件（默认关，不产生开销）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

from ..config import JudgeConfig
from ..llm_stream import build_openai_client, stream_chat_completion
from ..observability import bind_chain_context, current_context, log_event
from ..paths import resolve_project_path
from .prompts import persona_text

logger = logging.getLogger("auto_eval.judge")
_trace_lock = threading.Lock()

_TRACE_FIELDS = {
    "task_id",
    "session_name",
    "request_id",
    "item_index",
    "item_sequence",
    "ts",
    "status",
    "judge",
    "model",
    "system",
    "user",
    "rounds",
    "llm_rounds",
    "image_refs",
    "messages",
    "error",
    "error_type",
    "round",
}


def merge_trace_web_result(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """把 Web 最终结果平铺进模型调用记录，同时保留模型层审计字段。"""
    llm_rounds = record.get("llm_rounds") or []
    final_content = ""
    for llm_round in reversed(llm_rounds):
        content = llm_round.get("content") if isinstance(llm_round, dict) else None
        if content:
            final_content = str(content)
            break
    merged = dict(record)
    merged.update({
        "model_raw_output": final_content,
        "result_recorded_at": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime()
        ),
    })
    for field, value in result.items():
        if field in _TRACE_FIELDS and field in merged and merged[field] != value:
            merged[f"call_{field}"] = merged[field]
        merged[field] = value
    return merged


def _append_trace_record(trace_path: str, record: dict[str, Any]) -> bool:
    try:
        directory = os.path.dirname(trace_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _trace_lock:
            with open(trace_path, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logger.exception("写入裁判调用日志失败: path=%s", trace_path)
        return False


def flush_web_trace_records(
    records: list[tuple[str, dict[str, Any]]],
    result: dict[str, Any],
) -> int:
    """将一题暂存的模型调用记录连同 Web 最终结果一起落盘。"""
    written = 0
    for trace_path, record in records:
        if _append_trace_record(trace_path, merge_trace_web_result(record, result)):
            written += 1
    return written


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


def _redact_image_urls(messages: list[dict], refs: list[str] | None) -> list[dict]:
    """把 messages 里 image_url 的 base64 data url 替换成帧路径引用，避免 trace 文件膨胀。

    任务类评测每帧 base64 约 30KB，N 帧会让 judge_calls.jsonl 单行膨胀到 MB 级。
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
        # 明细日志路径：优先构造参数，其次环境变量；都不给则不记录
        _trace_path = trace_path or os.environ.get("AUTO_EVAL_JUDGE_TRACE")
        self.trace_path = str(resolve_project_path(_trace_path)) if _trace_path else None
        self._closed = False

    async def aclose(self) -> None:
        """关闭底层 AsyncOpenAI（httpx 连接池 + SSL 会话）。

        每次任务/更新批都会新建一套客户端，不关闭则依赖 GC 兜底回收连接池，
        长驻服务下 FD 与内存单调上涨。幂等，可安全重复调用。
        """
        if self._closed:
            return
        self._closed = True
        close = getattr(self.client, "close", None)  # AsyncOpenAI.close 为协程
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

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
                       user_image_refs: list[str] | None = None) -> str:
        """单轮生成裁判输出（多模态：传入关键帧 data_url 时 user content 为 [text, image_url...]）。"""
        user_content: Any = user
        if user_images:
            user_content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in user_images
            ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        judge_label = f"{self.cfg.display or self.cfg.name}({self.cfg.name})"
        with bind_chain_context(
            module="模型裁判", judge=judge_label, round=1
        ):
            kwargs = {"model": self.model, "messages": messages, **self._sampling_kwargs()}
            resp = await self._llm_create(kwargs, stream_callback=stream_callback)
        msg = resp.choices[0].message
        content = msg.content or ""

        if self.trace_path:
            self._write_trace({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "status": "success",
                "judge": self.cfg.name,
                "model": self.model,
                "system": system,
                "user": user,
                "rounds": 1,
                "llm_rounds": [{
                    "round": 1,
                    "content": content,
                    "tool_calls": [],
                    "finish_reason": getattr(resp.choices[0], "finish_reason", None),
                    "usage": _usage_dict(getattr(resp, "usage", None)),
                }],
                # trace 不存 base64（每帧 ~30KB×N 会让 jsonl 膨胀），image_url 换成帧路径引用
                "image_refs": user_image_refs,
                "messages": _redact_image_urls(messages, user_image_refs),
            })

        return content

    def _write_trace(self, detail: dict[str, Any]) -> None:
        assert self.trace_path
        try:
            ctx = current_context()
            record = {
                "task_id": ctx.task_id,
                "session_name": ctx.session_name,
                "request_id": ctx.request_id,
                "item_id": ctx.item_id,
                "item_index": ctx.item_index,
                "item_sequence": ctx.item_index + 1 if ctx.item_index >= 0 else None,
                **detail,
            }
            if ctx.judge_trace_callback:
                ctx.judge_trace_callback(self.trace_path, record)
            else:
                _append_trace_record(self.trace_path, record)
        except Exception:
            # 日志失败不应影响评测主流程
            logger.exception("写入裁判调用日志失败: path=%s", self.trace_path)

    def _sampling_kwargs(self) -> dict:
        """构造采样参数：只包含配置里非空的项（None=不发送，避免不支持该参数的网关 400）。"""
        k: dict = {"temperature": self.cfg.temperature}
        if self.cfg.top_p is not None:
            k["top_p"] = self.cfg.top_p
        if self.cfg.seed is not None:
            k["seed"] = self.cfg.seed
        return k

    async def _llm_create(self, kwargs: dict, max_attempts: int | None = None,
                          stream_callback: Callable[[str], None] | None = None):
        """始终使用流式接口；callback 只负责可选的前端分片通知。"""
        try:
            return await self._llm_create_stream(
                kwargs,
                callback=stream_callback,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            if self.trace_path:
                self._write_trace({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "status": "error",
                    "judge": self.cfg.name,
                    "model": self.model,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "round": current_context().round,
                })
            raise

    async def _llm_create_stream(
        self,
        kwargs: dict,
        callback: Callable[[str], None] | None = None,
        max_attempts: int | None = None,
    ):
        """流式调用 LLM，逐 token 回调，同时累积完整响应。"""
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
