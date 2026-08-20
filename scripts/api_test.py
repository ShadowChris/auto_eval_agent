"""最小 OpenAI 兼容接口流式连通性测试。

配置优先级：命令行参数 > 本文件顶部常量 > .env 环境变量。

推荐在项目根目录的 .env 中配置：

    API_TEST_BASE_URL=https://example.com/v1
    API_TEST_MODEL=your-model
    API_TEST_API_KEY=your-api-key

也可以直接修改下面三个常量。请勿把真实 API Key 提交到 Git。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI


# 可直接填写；留空时从 .env 读取。
BASE_URL = ""
MODEL = ""
API_KEY = ""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = "请只回复：连接成功"


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str
    prompt: str
    timeout: float


def _first_value(*values: str | None) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _env_value(primary: str, fallback: str) -> str:
    return _first_value(os.getenv(primary), os.getenv(fallback))


def resolve_settings(args: argparse.Namespace) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    base_url = _first_value(
        args.base_url,
        BASE_URL,
        _env_value("API_TEST_BASE_URL", "OPENAI_BASE_URL"),
    ).rstrip("/")
    model = _first_value(
        args.model,
        MODEL,
        _env_value("API_TEST_MODEL", "OPENAI_MODEL"),
    )
    api_key = _first_value(
        args.api_key,
        API_KEY,
        _env_value("API_TEST_API_KEY", "OPENAI_API_KEY"),
    )
    missing = [
        name
        for name, value in (("base_url", base_url), ("model", model))
        if not value
    ]
    if missing:
        raise ValueError(
            "缺少配置："
            + "、".join(missing)
            + "。请填写脚本顶部常量、命令行参数或 .env。"
        )
    return Settings(
        base_url=base_url,
        model=model,
        api_key=api_key or "EMPTY",
        prompt=args.prompt,
        timeout=args.timeout,
    )


def _masked_key(api_key: str) -> str:
    if api_key == "EMPTY":
        return "EMPTY（未配置）"
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]}"


def stream_test(settings: Settings) -> None:
    print("===== OpenAI 兼容接口流式连通性测试 =====")
    print(f"URL：{settings.base_url}")
    print(f"模型：{settings.model}")
    print(f"API Key：{_masked_key(settings.api_key)}")
    print(f"问题：{settings.prompt}")
    print("\n模型输出：", end="", flush=True)

    client = OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout,
    )
    started_at = time.perf_counter()
    first_chunk_at: float | None = None
    has_text = False
    try:
        stream = client.chat.completions.create(
            model=settings.model,
            messages=[{"role": "user", "content": settings.prompt}],
            stream=True,
        )
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = delta.content or getattr(delta, "reasoning_content", None) or ""
                if not text:
                    continue
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                has_text = True
                print(text, end="", flush=True)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
    finally:
        client.close()

    finished_at = time.perf_counter()
    if not has_text:
        print("（流式请求成功，但没有返回文本）", end="")
    print("\n\n===== 测试成功 =====")
    if first_chunk_at is not None:
        print(f"首个文本分片：{first_chunk_at - started_at:.2f} 秒")
    print(f"总耗时：{finished_at - started_at:.2f} 秒")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="测试 OpenAI 兼容模型接口的流式连通性")
    parser.add_argument("--base-url", help="OpenAI 兼容接口地址，例如 https://example.com/v1")
    parser.add_argument("--model", help="模型名称或 endpoint ID")
    parser.add_argument("--api-key", help="API Key；不建议在共享终端历史中明文传入")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="测试问题")
    parser.add_argument("--timeout", type=float, default=60.0, help="请求超时秒数，默认 60")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = resolve_settings(args)
        stream_test(settings)
        return 0
    except APIStatusError as exc:
        print(f"\n请求失败：HTTP {exc.status_code}", file=sys.stderr)
        response_text = getattr(exc.response, "text", "")
        if response_text:
            print(f"原始响应：{response_text}", file=sys.stderr)
    except APIConnectionError as exc:
        print(f"\n连接失败：{exc}", file=sys.stderr)
    except Exception as exc:
        print(f"\n测试失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
