"""搜索工具连通性测试 —— 测试 tools.py 中 web_search 各 provider 是否能正常调用。

用法:
    pytest tests/test_search_tools.py -v -s              # 运行所有测试
    pytest tests/test_search_tools.py -k "google" -v -s  # 只测 google 内部搜索
    pytest tests/test_search_tools.py -k "plan" -v -s    # 只测 search_plan_internal
"""
import json
import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

# 加载 .env（pytest 不会自动加载，而 @skipif 在 import 时就检查环境变量）
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

from auto_eval.judges.tools import (
    web_search,
    _search_google,
    _search_search_plan_internal,
    _search_tavily,
    _SEARCH_CACHE,
    _SEARCH_TTL,
    _KEY_ENV,
    calculate,
    fetch_page,
)


TEST_QUERY = "中国最长的河流"


# ============================================================
# 配置检查
# ============================================================

def test_key_env_mapping():
    """验证所有 provider 在 _KEY_ENV 中都有对应的环境变量名。"""
    expected = {"tavily", "serpapi", "bing", "jina", "google", "search_plan_internal"}
    actual = set(_KEY_ENV.keys())
    assert actual == expected, f"缺少 provider: {expected - actual}"


def test_no_hardcoded_secrets():
    """验证 tools.py 中没有硬编码 API Key（所有 key 必须走环境变量）。"""
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "auto_eval", "judges", "tools.py"),
              encoding="utf-8") as f:
        content = f.read()
    import re
    hardcoded_keys = re.findall(r'[=]["\']([0-9A-Fa-f]{32,})["\']', content)
    assert not hardcoded_keys, f"发现硬编码 API Key: {hardcoded_keys}, 必须改为环境变量"


# ============================================================
# 内部搜索连通性测试（需要配置环境变量）
# ============================================================

@pytest.mark.skipif(
    not os.environ.get("GOOGLE_INTERNAL_API_KEY"),
    reason="需要设置 GOOGLE_INTERNAL_API_KEY 环境变量",
)
def test_google_search_direct():
    """直接调用 _search_google，测试内部 Google 搜索代理连通性。"""
    key = os.environ["GOOGLE_INTERNAL_API_KEY"]
    results = _search_google(TEST_QUERY, 3, key)

    print(f"\n[google] 查询: {TEST_QUERY}")
    print(f"[google] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r.get('title', '')[:60]} | {r.get('snippet', '')[:60]}")

    assert len(results) > 0, "google 搜索应返回至少一条结果"
    assert any("长江" in r.get("title", "") + r.get("snippet", "") for r in results), 'Google 搜索结果应包含"长江"'


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_INTERNAL_API_KEY"),
    reason="需要设置 GOOGLE_INTERNAL_API_KEY 环境变量",
)
def test_google_search_via_web_search():
    """通过 web_search() 入口测试 google provider 连通性。"""
    results = web_search(TEST_QUERY, providers="google", topk=3)

    print(f"\n[web_search -> google] 查询: {TEST_QUERY}")
    print(f"[web_search -> google] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r[:120]}")

    assert len(results) > 0, "web_search(google) 应返回至少一条结果"


@pytest.mark.skipif(
    not os.environ.get("TAVILY_API_KEY"),
    reason="需要设置 TAVILY_API_KEY 环境变量",
)
def test_tavily_search_direct():
    """直接调用 _search_tavily，测试 Tavily API 连通性。"""
    key = os.environ["TAVILY_API_KEY"]
    results = _search_tavily(TEST_QUERY, 3, key)

    print(f"\n[tavily] 查询: {TEST_QUERY}")
    print(f"[tavily] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r.get('title', '')[:60]} | {r.get('snippet', '')[:60]}")

    assert len(results) > 0, "tavily 搜索应返回至少一条结果"
    r = results[0]
    assert isinstance(r, dict), f"每条结果应为 dict，实际: {type(r)}"
    assert r.get("title") or r.get("snippet"), "应包含 title 或 snippet"


@pytest.mark.skipif(
    not os.environ.get("TAVILY_API_KEY"),
    reason="需要设置 TAVILY_API_KEY 环境变量",
)
def test_tavily_search_via_web_search():
    """通过 web_search() 入口测试 tavily provider 连通性。"""
    results = web_search(TEST_QUERY, providers="tavily", topk=3)

    print(f"\n[web_search -> tavily] 查询: {TEST_QUERY}")
    print(f"[web_search -> tavily] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r[:120]}")

    assert len(results) > 0, "web_search(tavily) 应返回至少一条结果"


@pytest.mark.skipif(
    not os.environ.get("SEARCH_PLAN_INTERNAL_API_KEY"),
    reason="需要设置 SEARCH_PLAN_INTERNAL_API_KEY 环境变量",
)
def test_search_plan_internal_direct():
    """直接调用 _search_search_plan_internal，测试搜索规划引擎连通性。"""
    key = os.environ["SEARCH_PLAN_INTERNAL_API_KEY"]
    results = _search_search_plan_internal(TEST_QUERY, 3, key)

    print(f"\n[search_plan_internal] 查询: {TEST_QUERY}")
    print(f"[search_plan_internal] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r.get('title', '')[:60]} | {r.get('snippet', '')[:60]}")

    assert len(results) > 0, "search_plan_internal 应返回至少一条结果"


@pytest.mark.skipif(
    not os.environ.get("SEARCH_PLAN_INTERNAL_API_KEY"),
    reason="需要设置 SEARCH_PLAN_INTERNAL_API_KEY 环境变量",
)
def test_search_plan_internal_via_web_search():
    """通过 web_search() 入口测试 search_plan_internal provider 连通性。"""
    results = web_search(TEST_QUERY, providers="search_plan_internal", topk=3)

    print(f"\n[web_search -> search_plan_internal] 查询: {TEST_QUERY}")
    print(f"[web_search -> search_plan_internal] 结果数: {len(results)}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r[:120]}")

    assert len(results) > 0, "web_search(search_plan_internal) 应返回至少一条结果"


# ============================================================
# 缓存测试
# ============================================================

def test_search_cache_works():
    """验证相同 providers+query+topk 在 TTL 内复用结果。"""
    _SEARCH_CACHE.clear()

    providers = "tavily"
    ck = f"{providers}:{TEST_QUERY}:2"

    cached_result = ["长江：中国最长的河流，全长约6300公里"]
    _SEARCH_CACHE[ck] = (time.monotonic(), cached_result)

    results = web_search(TEST_QUERY, providers=providers, topk=2)
    assert results == cached_result, "缓存应返回之前保存的结果"


def test_search_cache_expires():
    """验证超过 TTL 后缓存失效。"""
    _SEARCH_CACHE.clear()
    providers = "tavily"
    ck = f"{providers}:test_expire:1"

    old_time = time.monotonic() - _SEARCH_TTL - 60
    _SEARCH_CACHE[ck] = (old_time, ["old data"])

    key = os.environ.get("BING_API_KEY")
    if not key:
        results = web_search("test_expire", providers=providers, topk=1)
        assert results != ["old data"], "过期缓存不应被使用"
    else:
        pytest.skip("有 BING_API_KEY 时跳过缓存过期测试")


# ============================================================
# 异常/边界情况测试
# ============================================================

def test_no_provider():
    """不传 provider 应返回空列表。"""
    assert web_search("任何查询") == []


def test_unknown_provider():
    """未知 provider 应返回空列表（无 API Key 映射）。"""
    results = web_search("测试", providers="unknown_provider_xyz")
    assert results == []


def test_missing_api_key():
    """provider 存在但未设置 API Key 应返回空列表。"""
    provider = "serpapi"
    if os.environ.get("SERPAPI_API_KEY"):
        provider = "bing"
    if os.environ.get("BING_API_KEY"):
        provider = "tavily"
    if os.environ.get("TAVILY_API_KEY"):
        pytest.skip("所有外部 API Key 都已配置，跳过无 key 测试")

    results = web_search("测试", providers=provider, topk=1)
    assert results == [], f"未配置 {_KEY_ENV[provider]} 时应返回空列表"


def test_empty_query():
    """空 query 应返回空列表（无异常）。"""
    results = web_search("", providers="google", topk=1)
    assert results == []


# ============================================================
# 其他工具快速验证
# ============================================================

def test_calculate():
    """calculate 工具基本功能。"""
    assert calculate("1+1") == "2"
    assert calculate("17*24") == "408"
    assert calculate("(3+5)*2") == "16"


def test_calculate_error():
    """calculate 异常输入应返回错误信息而非抛异常。"""
    result = calculate("1/0")
    assert result.startswith("("), f"除以零应返回错误信息，实际: {result}"


def test_fetch_page_invalid_url():
    """fetch_page 无效 URL 应返回错误信息而非抛异常。"""
    result = fetch_page("http://invalid-url-xyz-123456.com")
    assert result.startswith("(抓取失败:"), f"无效 URL 应返回错误信息，实际: {result}"


# ============================================================
# 全 provider 列表测试（只打印状态，不强制成功）
# ============================================================

@pytest.mark.parametrize("provider,env_key", [
    ("tavily", "TAVILY_API_KEY"),
    ("serpapi", "SERPAPI_API_KEY"),
    ("bing", "BING_API_KEY"),
    ("jina", "JINA_API_KEY"),
    ("google", "GOOGLE_INTERNAL_API_KEY"),
    ("search_plan_internal", "SEARCH_PLAN_INTERNAL_API_KEY"),
])
def test_all_providers_status(provider, env_key, capsys):
    """列出所有 provider 的配置状态，帮助排查。"""
    key_set = bool(os.environ.get(env_key))
    status = "已配置" if key_set else "未配置（跳过）"
    print(f"\n[{status}] {provider} -> {env_key}")

    if not key_set:
        return

    results = web_search(TEST_QUERY, providers=provider, topk=2)
    if results:
        print(f"   结果数: {len(results)}")
        print(f"   第一条: {results[0][:100]}")
    else:
        print(f"   结果数: 0（API 调用失败或返回空）")


if __name__ == "__main__":
    """直接运行：打印所有 provider 状态，跳过无 key 的 provider。"""
    print("=" * 60)
    print("搜索工具连通性检查")
    print("=" * 60)

    providers = [
        ("tavily", "TAVILY_API_KEY"),
        ("serpapi", "SERPAPI_API_KEY"),
        ("bing", "BING_API_KEY"),
        ("jina", "JINA_API_KEY"),
        ("google", "GOOGLE_INTERNAL_API_KEY"),
        ("search_plan_internal", "SEARCH_PLAN_INTERNAL_API_KEY"),
    ]

    for provider, env_key in providers:
        key = os.environ.get(env_key, "")
        if not key:
            print(f"  ..  {provider:25s} -> {env_key:30s}  未配置，跳过")
            continue
        print(f"  >>  {provider:25s} -> {env_key:30s}  测试中...", end=" ")
        try:
            results = web_search(TEST_QUERY, providers=provider, topk=2)
            if results:
                print(f"OK {len(results)} 条结果")
            else:
                print(".. 返回空结果")
        except Exception as e:
            print(f"XX {e}")

    print("=" * 60)
