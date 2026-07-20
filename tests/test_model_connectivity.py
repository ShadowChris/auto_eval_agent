"""当前配置中各模型入口的真实连通性测试。

默认跳过，避免日常单元测试消耗模型额度。手动执行：
RUN_LIVE_LLM_TESTS=1 python -m pytest -q -m integration tests/test_model_connectivity.py -s
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_eval.config import load_config
from auto_eval.llm_stream import build_openai_client, stream_chat_completion


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
CONFIG = load_config(PROJECT_ROOT / "config")


@dataclass(frozen=True)
class ModelTarget:
    module: str
    name: str
    base_url: str
    model: str
    api_key_env: str | None
    temperature: float
    include_usage: bool
    connect_timeout_s: float
    read_timeout_s: float
    total_timeout_s: float

    @property
    def test_id(self) -> str:
        return f"{self.module}-{self.name}-{self.model}"


def _targets() -> list[ModelTarget]:
    targets: list[ModelTarget] = []

    for cfg in CONFIG.models:
        if cfg.runner != "openai_compat":
            continue
        targets.append(
            ModelTarget(
                module="被测模型",
                name=cfg.name,
                base_url=cfg.base_url or "",
                model=cfg.model or cfg.name,
                api_key_env=cfg.api_key_env,
                temperature=cfg.temperature,
                include_usage=cfg.stream_include_usage,
                connect_timeout_s=cfg.connect_timeout_s,
                read_timeout_s=cfg.read_timeout_s,
                total_timeout_s=cfg.total_timeout_s,
            )
        )

    for cfg in CONFIG.judges:
        if cfg.runner != "openai_compat":
            continue
        targets.append(
            ModelTarget(
                module="裁判模型",
                name=cfg.name,
                base_url=cfg.base_url or "",
                model=cfg.model or cfg.name,
                api_key_env=cfg.api_key_env,
                temperature=cfg.temperature,
                include_usage=cfg.stream_include_usage,
                connect_timeout_s=cfg.connect_timeout_s,
                read_timeout_s=cfg.read_timeout_s,
                total_timeout_s=cfg.total_timeout_s,
            )
        )

    options = CONFIG.eval_options
    if options.classify_model:
        first_judge = CONFIG.judges[0] if CONFIG.judges else None
        targets.append(
            ModelTarget(
                module="分类模型",
                name="classify_model",
                base_url=options.classify_base_url
                or (first_judge.base_url if first_judge else "")
                or "",
                model=options.classify_model,
                api_key_env=options.classify_api_key_env
                or (first_judge.api_key_env if first_judge else None),
                temperature=0,
                include_usage=True,
                connect_timeout_s=min(
                    first_judge.connect_timeout_s if first_judge else 10.0,
                    10.0,
                ),
                read_timeout_s=15.0,
                total_timeout_s=15.0,
            )
        )

    return targets


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM_TESTS") != "1",
        reason="设置 RUN_LIVE_LLM_TESTS=1 后才执行真实模型连通性测试",
    ),
]


@pytest.mark.parametrize(
    "target",
    [pytest.param(target, id=target.test_id) for target in _targets()],
)
async def test_configured_model_connectivity(target: ModelTarget):
    assert target.base_url, f"{target.module}[{target.name}] 未配置 base_url"
    assert "your-proxy" not in target.base_url, (
        f"{target.module}[{target.name}] 仍使用占位地址：{target.base_url}"
    )

    api_key = os.environ.get(target.api_key_env or "") if target.api_key_env else None
    if target.api_key_env and not api_key:
        pytest.skip(f"缺少环境变量 {target.api_key_env}")

    client = build_openai_client(
        base_url=target.base_url,
        api_key=api_key or "EMPTY",
        connect_timeout_s=target.connect_timeout_s,
        read_timeout_s=target.read_timeout_s,
    )
    started = time.perf_counter()
    try:
        response = await stream_chat_completion(
            client,
            {
                "model": target.model,
                "messages": [
                    {
                        "role": "user",
                        "content": "这是连通性测试。请只回复：OK",
                    }
                ],
                "temperature": target.temperature,
                "max_tokens": 256,
            },
            include_usage=target.include_usage,
            total_timeout_s=min(target.total_timeout_s, 60.0),
            max_attempts=1,
        )
    finally:
        await client.close()

    elapsed = time.perf_counter() - started
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    assert content, (
        f"{target.module}[{target.name}] 返回空内容，"
        f"finish_reason={choice.finish_reason}"
    )

    usage = response.usage
    print(
        f"\n[{target.module}] {target.name} | model={target.model} | "
        f"finish={choice.finish_reason} | elapsed={elapsed:.2f}s | "
        f"input_tokens={getattr(usage, 'prompt_tokens', None)} | "
        f"output_tokens={getattr(usage, 'completion_tokens', None)} | "
        f"content={content[:80]!r}"
    )
