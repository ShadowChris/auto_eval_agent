"""批跑调度：题 × 模型 笛卡尔积，并发 + 限流 + 重试 + 断点续跑。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ..config import ModelConfig
from ..dataset import to_prompt
from ..runners import BaseRunner, build_runner
from ..schema import EvalItem, ModelOutput
from .checkpoint import AnswerStore
from .ratelimit import RateLimiter
from .retry import retry_call


class Orchestrator:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)

    async def run(
        self,
        items: list[EvalItem],
        model_cfgs: list[ModelConfig],
        on_progress=None,
    ) -> dict[str, list[ModelOutput]]:
        runners: list[BaseRunner] = [build_runner(c) for c in model_cfgs]
        limiters = {c.name: RateLimiter(c.concurrency, c.rpm) for c in model_cfgs}
        stores = {c.name: AnswerStore(self.run_dir, c.name) for c in model_cfgs}

        # 断点续跑：已有成功的 item 跳过
        done = {name: store.done_ids() for name, store in stores.items()}

        total = sum(
            1 for item in items for runner in runners if item.id not in done[runner.name]
        )
        completed = 0

        async def _track(item: EvalItem, runner: BaseRunner) -> ModelOutput:
            nonlocal completed
            out = await self._run_one(item, runner, limiters[runner.name], stores[runner.name])
            stores[runner.name].append(out)
            completed += 1
            if on_progress:
                on_progress(completed, total, runner.name, item.id)
            return out

        wrapped = [
            _track(item, runner)
            for item in items
            for runner in runners
            if item.id not in done[runner.name]
        ]
        if wrapped:
            await asyncio.gather(*wrapped)

        return {name: store.load_all() for name, store in stores.items()}

    async def _run_one(self, item: EvalItem, runner: BaseRunner, limiter: RateLimiter, store: AnswerStore) -> ModelOutput:
        prompt = to_prompt(item)
        await limiter.acquire()
        try:
            try:
                if runner.handles_retries:
                    out = await runner.generate_strict(prompt, item_id=item.id)
                else:
                    out = await retry_call(
                        lambda: runner.generate_strict(prompt, item_id=item.id)
                    )
                return out
            except Exception as e:
                # 重试耗尽仍失败：记录 error，留待后续（可补跑或统计失败率）
                return ModelOutput(
                    item_id=item.id,
                    model=runner.name,
                    answer="",
                    error=f"{type(e).__name__}: {e}",
                )
        finally:
            limiter.release()


def default_progress(done: int, total: int, model: str, item_id: str):
    if total == 0:
        return
    bar_len = 24
    filled = int(bar_len * done / total)
    bar = "█" * filled + "·" * (bar_len - filled)
    sys.stdout.write(f"\r批跑 [{bar}] {done}/{total} ({model}/{item_id[:12]})   ")
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")
