"""探针脚本：实测各裁判/被测模型端点对随机性参数的支持情况。

目的：去掉对"某端点强制 temperature=1"的猜测。对每个端点跑 3 个变体：
  1. temperature=0          —— 端点是否接受低温采样？
  2. temperature=0 + top_p=0.1 + seed=42 —— top_p / seed 是否被接受？
  3. 变体 1 连跑 2 次        —— 输出是否逐字一致（端点是否真确定性）？

每个变体 max_attempts=1，遇到 4xx 立刻抛错，从而能区分"接受/拒绝"。

运行：python probe_sampling.py
"""
import asyncio
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from auto_eval.config import load_config  # noqa: E402
from auto_eval.llm_stream import build_openai_client, stream_chat_completion  # noqa: E402

CONFIG_DIR = Path(__file__).resolve().parent / "config"
PROMPT = "只回复两个字：你好。不要任何额外内容。"
MAX_TOKENS = 16


def _describe_exc(exc: BaseException) -> str:
    """从异常里提取状态码与简短信息，便于判断是否 4xx 拒绝。"""
    code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    msg = getattr(exc, "message", None) or str(exc)
    # SDK 的 APIStatusError 常把 body 塞在 exc.body
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or msg
    snippet = str(msg).replace("\n", " ").strip()
    if len(snippet) > 180:
        snippet = snippet[:180] + "…"
    return f"HTTP {code} | {snippet}" if code else snippet


async def _one_call(client, model, extra):
    """跑一次，返回 (ok, text_or_errdesc)。max_attempts=1 让 4xx 直接抛出。"""
    req = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
    }
    req.update(extra)
    try:
        resp = await stream_chat_completion(client, req, max_attempts=1, total_timeout_s=60.0)
        text = (getattr(resp.choices[0].message, "content", "") or "").strip()
        return True, text
    except Exception as exc:  # noqa: BLE001 —— 探针要捕获所有错误来判定端点行为
        return False, _describe_exc(exc)


async def probe_endpoint(label, base_url, api_key, model):
    print(f"\n{'='*72}")
    print(f"端点：{label}\n      base_url={base_url}\n      model={model}")
    if not api_key:
        print("  ⚠ 跳过：未取到 API key（检查 .env 的 *_API_KEY 环境变量名）")
        return
    if not base_url or "your-proxy" in base_url or ":port" in base_url:
        print("  ⚠ 跳过：base_url 是占位符，请在 models.yaml 填真实地址")
        return

    try:
        client = build_openai_client(
            base_url=base_url, api_key=api_key, connect_timeout_s=10.0, read_timeout_s=60.0
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 跳过：base_url 非法，无法建客户端 → {exc}")
        return

    # 变体 1：temperature=0
    ok0, out0 = await _one_call(client, model, {"temperature": 0})
    print(f"  [1] temperature=0           : {'✓ 接受' if ok0 else '✗ 拒绝'}")
    print(f"      → {out0 if ok0 else out0}")

    # 变体 2：temperature=0 + top_p + seed
    ok1, out1 = await _one_call(
        client, model, {"temperature": 0, "top_p": 0.1, "seed": 42}
    )
    print(f"  [2] +top_p=0.1,+seed=42     : {'✓ 接受' if ok1 else '✗ 拒绝'}")
    print(f"      → {out1 if ok1 else out1}")

    # 变体 3：可复现性（temperature=0 连跑两次比一致性）
    if ok0:
        ok3, out3 = await _one_call(client, model, {"temperature": 0})
        same = ok3 and (out0 == out3)
        print(
            f"  [3] 复跑一致性(temp=0)      : "
            f"{'✓ 逐字一致' if same else '≈ 不完全一致（低温仍非比特确定）'}"
        )
        if not same:
            print(f"      第一次：{out0}")
            print(f"      第二次：{out3 if ok3 else '<err>'}")

    # 变体 4/5：当端点强制 temperature=1 时，看 top_p / seed 是否仍能单独生效
    if not ok0:
        ok4, out4 = await _one_call(client, model, {"temperature": 1, "top_p": 0.1})
        print(f"  [4] temp=1 +top_p=0.1       : {'✓ 接受' if ok4 else '✗ 拒绝'}")
        if not ok4:
            print(f"      → {out4}")
        ok5, out5 = await _one_call(client, model, {"temperature": 1, "seed": 42})
        print(f"  [5] temp=1 +seed=42         : {'✓ 接受' if ok5 else '✗ 拒绝'}")
        if not ok5:
            print(f"      → {out5}")


async def main():
    cfg = load_config(CONFIG_DIR)

    # 收集端点并按 (base_url, model) 去重：裁判 + 被测模型
    seen: set[tuple[str, str]] = set()
    endpoints: list[tuple[str, str, str | None, str | None]] = []

    for j in cfg.judges:
        key = (j.base_url or "", j.model or "")
        if key in seen or not j.base_url:
            continue
        seen.add(key)
        endpoints.append((f"裁判 {j.display or j.name}", j.base_url, j.api_key(), j.model))

    for m in cfg.models:
        if m.runner != "openai_compat":
            continue
        key = (m.base_url or "", m.model or "")
        if key in seen or not m.base_url:
            continue
        seen.add(key)
        endpoints.append((f"被测模型 {m.name}", m.base_url, m.api_key(), m.model))

    if not endpoints:
        print("未发现任何 openai_compat 端点可探测。")
        return

    print(f"共 {len(endpoints)} 个端点待探测。每个端点最多 3 次小请求（max_tokens={MAX_TOKENS}）。")

    for label, base_url, api_key, model in endpoints:
        await probe_endpoint(label, base_url, api_key, model)

    print("\n" + "=" * 72)
    print("判定指引：")
    print("  - [1]✗ → 该端点确实不接受 temperature=0；保留 temperature=1 并改用 top_p 降噪。")
    print("  - [1]✓ → 可直接在 judges.yaml 里设低 temperature。")
    print("  - [2]✓ → top_p / seed 可放心配置（[2]✗ 则别加 seed，部分网关会 400）。")


if __name__ == "__main__":
    asyncio.run(main())
