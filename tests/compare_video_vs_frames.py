"""对比 kimi-for-coding-openai（代理）两种视频理解方式：video_url 直传 vs 关键帧 image_url。
同一视频、同一 describe 任务，对比时延 / token / 输出质量。
"""
import asyncio, base64, os, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from openai import AsyncOpenAI
from auto_eval.media import video_to_frame_urls

import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--endpoint", choices=["proxy", "official"], default="proxy")
_args = _ap.parse_args()
if _args.endpoint == "official":
    BASE, KEY, MODEL = "https://api.moonshot.cn/v1", os.environ["KIMI_API_KEY"], "kimi-k2.6"
else:
    BASE = os.environ.get("PROXY_BASE_URL", "http://1239mxgn96959.vicp.fun:4009/v1")
    KEY = os.environ["PROXY_API_KEY"]
    MODEL = "kimi-for-coding-openai"
print(f"endpoint={_args.endpoint}  base={BASE}  model={MODEL}")
VIDEO = Path(__file__).resolve().parent.parent / "data" / "闹钟操作录频.mp4"
PROMPT = "这是手机操作录屏。请按步骤详细描述用户/agent 依次做了哪些操作、最终界面停在哪里。"


async def call(client, messages, label):
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=MODEL, messages=messages, temperature=1, max_tokens=4096,
        )
    except Exception as e:
        print(f"\n===== [{label}] 失败 =====")
        print(f"  {str(e)[:300]}")
        return
    dt = time.perf_counter() - t0
    msg = resp.choices[0].message
    content = (getattr(msg, "content", None) or "").strip()
    rc = (getattr(msg, "reasoning_content", None) or "")
    u = resp.usage
    print(f"\n===== [{label}] =====")
    print(f"耗时: {dt:.1f}s | prompt_tokens={u.prompt_tokens} completion={u.completion_tokens} "
          f"reasoning={getattr(u.completion_tokens_details,'reasoning_tokens',0)} | finish={resp.choices[0].finish_reason}")
    print("--- 输出 ---")
    print(content[:1400] or "(content 为空)")


async def main():
    client = AsyncOpenAI(base_url=BASE, api_key=KEY, timeout=300)
    print(f"视频: {VIDEO.name} ({VIDEO.stat().st_size//1024} KB)")

    # 方案 A：video_url 直传（base64）
    print("\n编码 video_url (base64)...")
    b64 = base64.b64encode(VIDEO.read_bytes()).decode()
    print(f"base64 ≈ {len(b64)//1024} KB")
    msgA = [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
        {"type": "text", "text": PROMPT},
    ]}]
    await call(client, msgA, "A: video_url 直传")

    # 方案 B：关键帧 image_url（场景检测抽帧）
    print("\n抽帧中（场景检测）...")
    frames = video_to_frame_urls(VIDEO)
    print(f"抽帧 {len(frames)} 张")
    parts = [{"type": "image_url", "image_url": {"url": u}} for u in frames]
    parts.append({"type": "text", "text": "以上是同一段手机操作录屏按时间顺序抽出的关键帧。" + PROMPT})
    await call(client, [{"role": "user", "content": parts}], "B: 关键帧 image_url")


asyncio.run(main())
