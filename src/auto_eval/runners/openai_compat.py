"""OpenAI 兼容 runner —— 豆包(火山 Ark)及其他兼容厂商走这个。

火山 Ark：base_url=https://ark.cn-beijing.volces.com/api/v3，api_key=$ARK_API_KEY，model=endpoint_id。
"""
from __future__ import annotations

from ..llm_stream import build_openai_client, stream_chat_completion
from .base import BaseRunner


class OpenAICompatRunner(BaseRunner):
    handles_retries = True

    def __init__(self, cfg):
        super().__init__(cfg)
        if not cfg.base_url:
            raise ValueError(f"openai_compat runner[{cfg.name}] 缺少 base_url")
        self.client = build_openai_client(
            base_url=cfg.base_url,
            api_key=cfg.api_key() or "EMPTY",
            connect_timeout_s=cfg.connect_timeout_s,
            read_timeout_s=cfg.read_timeout_s,
        )
        self.model = cfg.model or cfg.name

    async def _call(self, prompt: str, **kw) -> dict:
        resp = await stream_chat_completion(
            self.client,
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.cfg.temperature,
                "max_tokens": self.cfg.max_tokens or 4096,
            },
            include_usage=self.cfg.stream_include_usage,
            total_timeout_s=self.cfg.total_timeout_s,
            max_attempts=self.cfg.max_attempts,
            retry_base_s=self.cfg.retry_base_s,
            retry_max_s=self.cfg.retry_max_s,
        )
        choice = resp.choices[0]
        answer = (getattr(choice.message, "content", "") or "").strip()
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "prompt_tokens", 0) if usage else 0
        tout = getattr(usage, "completion_tokens", 0) if usage else 0
        return {
            "answer": answer,
            "tokens_in": tin,
            "tokens_out": tout,
            "raw": {"model": self.model, "finish_reason": getattr(choice, "finish_reason", None)},
        }
