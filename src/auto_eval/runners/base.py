"""Runner 抽象基类与工厂。

所有被测模型（自研 agent + 竞品豆包等）统一为 `BaseRunner.generate(prompt)`，
返回 `ModelOutput`。Runner 只接收 `dataset.to_prompt(item)` 的结果，
结构上拿不到 reference（reference 隔离由 dataset 保证）。
"""
from __future__ import annotations

import abc
import time

from ..config import ModelConfig
from ..schema import ModelOutput


class BaseRunner(abc.ABC):
    handles_retries = False

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name

    @abc.abstractmethod
    async def _call(self, prompt: str, **kw) -> dict:
        """子类实现：返回 {answer, tokens_in?, tokens_out?, cost?, raw?}，失败抛异常。"""
        ...

    async def generate(self, prompt: str, *, item_id: str = "", **kw) -> ModelOutput:
        t0 = time.perf_counter()
        try:
            res = await self._call(prompt, **kw)
            latency = int((time.perf_counter() - t0) * 1000)
            return ModelOutput(
                item_id=item_id,
                model=self.name,
                answer=res.get("answer", "") or "",
                latency_ms=res.get("latency_ms", latency),
                tokens_in=res.get("tokens_in", 0),
                tokens_out=res.get("tokens_out", 0),
                cost=res.get("cost", 0.0),
                raw=res.get("raw", {}),
            )
        except Exception as e:  # 任意失败都记录为 error，不抛出（断点续跑靠 error 重试）
            latency = int((time.perf_counter() - t0) * 1000)
            return ModelOutput(
                item_id=item_id,
                model=self.name,
                answer="",
                latency_ms=latency,
                error=f"{type(e).__name__}: {e}",
            )

    async def generate_strict(self, prompt: str, *, item_id: str = "", **kw) -> ModelOutput:
        """与 generate 相同，但失败时抛异常（供批跑层做重试与错误归集）。"""
        t0 = time.perf_counter()
        res = await self._call(prompt, **kw)
        latency = int((time.perf_counter() - t0) * 1000)
        return ModelOutput(
            item_id=item_id,
            model=self.name,
            answer=res.get("answer", "") or "",
            latency_ms=res.get("latency_ms", latency),
            tokens_in=res.get("tokens_in", 0),
            tokens_out=res.get("tokens_out", 0),
            cost=res.get("cost", 0.0),
            raw=res.get("raw", {}),
        )


def build_runner(cfg: ModelConfig) -> BaseRunner:
    kind = cfg.runner
    if kind == "openai_compat":
        from .openai_compat import OpenAICompatRunner

        return OpenAICompatRunner(cfg)
    if kind == "http":
        from .http_runner import HttpRunner

        return HttpRunner(cfg)
    if kind == "func":
        from .func_runner import FuncRunner

        return FuncRunner(cfg)
    if kind == "cli":
        from .cli_runner import CliRunner

        return CliRunner(cfg)
    raise ValueError(f"未知 runner 类型：{kind}（支持 openai_compat/http/func/cli）")
