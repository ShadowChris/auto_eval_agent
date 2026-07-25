"""垂域分类模块效果测试。

使用项目真实的 AI 调用方式测试 _classify() 函数的分类效果。
数据来源：data/test_skills_with_doubao_all_sample2.jsonl

特性：
- 重试机制：支持指数退避重试，应对限流/过载
- Excel 输出：生成详细的测试报告 xlsx

用法：
    # 直接运行（推荐）
    python tests/test_domain_classify.py

    # 指定数据文件
    python tests/test_domain_classify.py --data data/other_test.jsonl

    # 限制测试数量
    python tests/test_domain_classify.py --limit 10

    # pytest 运行
    pytest tests/test_domain_classify.py -v
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from openai import AsyncOpenAI
import httpx

from auto_eval.config import load_config
from auto_eval.judges.skill_router import SkillRouter
from auto_eval.schema import EvalItem

# 加载环境变量
load_dotenv(ROOT / ".env", override=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_classify")


# =========================================================================== #
# 数据加载
# =========================================================================== #
def load_test_data(path: str | Path) -> list[dict[str, Any]]:
    """从 jsonl 加载测试数据。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在: {path}")

    items = []
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # 补充 id 字段（如果缺失）
                if "id" not in obj:
                    obj["id"] = f"test_{ln:04d}"
                items.append(obj)
            except json.JSONDecodeError as e:
                logger.warning(f"跳过非法 JSON 行 {ln}: {e}")
    return items


def to_eval_item(data: dict[str, Any]) -> EvalItem:
    """将测试数据转换为 EvalItem。"""
    return EvalItem(
        id=data.get("id", "unknown"),
        question=data.get("question", ""),
        context=data.get("context"),
        category=data.get("category", "default"),
    )


# =========================================================================== #
# 分类函数（参考 rubric_judge._classify）
# =========================================================================== #
def _skill_labels(skill_router: SkillRouter) -> list[tuple[str, str]]:
    """候选 (name, 展示文字)。"""
    if not skill_router:
        return []
    return [
        (s.name, s.display or s.name)
        for s in skill_router.domain.values()
        if s.name and s.name != "default"
    ]


def _normalize_label(text: str, labels: list[tuple[str, str]], fallback: str) -> str | None:
    """把 LLM 分类输出归一化为 skill name。"""
    if not text:
        return None
    text = text.strip()
    if fallback and fallback in text:
        return None
    # 精确匹配
    for name, disp in labels:
        if text == name or text == disp:
            return name
    # 子串匹配：取最长匹配
    best, best_len = None, 0
    for name, disp in labels:
        if name in text and len(name) > best_len:
            best, best_len = name, len(name)
        if disp in text and len(disp) > best_len:
            best, best_len = name, len(disp)
    return best


# few-shot 示例
_FEWSHOT: dict[str, list[str]] = {
    "digital_3c": ["哪些手机支持卫星通信？", "某手机今天发布了什么配置？"],
    "search": ["帮我找华为手机参数官网链接"],
    "news": ["今天有哪些重要科技行业新闻？"],
    "lbs_travel": ["规划上海三日游路线"],
    "document": ["总结这份PDF的结论"],
    "music": ["这首歌属于什么风格？"],
    "film_tv": ["这部电影适合儿童看吗？"],
    "automotive": ["宝马X5和奔驰GLE哪个更值得买？"],
    "sports": ["2026年世界杯冠军是谁？"],
    "math_solving": ["解方程 x^2 + 2x - 8 = 0"],
}


async def classify_item(
    item: EvalItem,
    client: AsyncOpenAI,
    model: str,
    skill_router: SkillRouter,
    max_attempts: int = 3,
) -> tuple[str | None, str, int]:
    """对单个 item 进行垂域分类（带重试机制）。

    Args:
        item: 评测项
        client: OpenAI 客户端
        model: 模型名
        skill_router: Skill 路由器
        max_attempts: 最大重试次数

    Returns:
        (skill_name, raw_response, attempts): 分类结果、原始响应、实际尝试次数
    """
    labels = _skill_labels(skill_router)
    if not labels:
        return None, "", 0

    default_skill = skill_router.domain.get("default")
    fallback = default_skill.display if default_skill and default_skill.display else "通用"
    shown = " / ".join(d for _, d in labels)

    # 构建 name -> display 映射
    name_to_disp: dict[str, str] = {}
    definitions = []
    for name, display in labels:
        skill = skill_router.domain.get(name)
        rule = (skill.rules or "").strip() if skill else ""
        definitions.append(f"- {display}：{rule or '按用户核心意图判断是否属于该类'}")
        name_to_disp[name] = display

    # 渲染 few-shot
    fewshot_lines: list[str] = []
    for skill_name, queries in _FEWSHOT.items():
        disp = name_to_disp.get(skill_name)
        if not disp:
            continue
        for q in queries:
            fewshot_lines.append(f'- "{q}" → {disp}')

    # 渲染分类原则
    disp_d3c = name_to_disp.get("digital_3c", "数码3C")
    disp_auto = name_to_disp.get("automotive", "汽车")
    disp_sports = name_to_disp.get("sports", "体育")
    disp_music = name_to_disp.get("music", "音乐")
    disp_film = name_to_disp.get("film_tv", "影视")
    disp_search = name_to_disp.get("search", "搜索")
    disp_news = name_to_disp.get("news", "新闻")
    disp_doc = name_to_disp.get("document", "文档")
    disp_lbs = name_to_disp.get("lbs_travel", "LBS（旅行规划）")

    system = (
        "你是查询意图分类器。请理解用户真正希望得到的结果，而不是只匹配关键词。\n"
        f"只能从以下标签中选择一个：{shown} / {fallback}。\n\n"
        "类别说明：\n" + "\n".join(definitions) + "\n"
        f"- {fallback}：无法明确归入上述类别的通用问答。\n\n"
        "分类原则：\n"
        "1. 优先按用户问题的核心对象和最终交付物分类，不按单个关键词分类。\n"
        f"2. 垂直主题优先：手机/电脑归{disp_d3c}，车型归{disp_auto}，赛事归{disp_sports}，歌曲归{disp_music}，影视作品归{disp_film}。\n"
        f"3. {disp_news}只用于公共事件、时政、财经和社会热点；某款手机或汽车的发布参数仍归对应垂域。\n"
        f"4. {disp_search}只用于用户明确要求找网页、链接、资料、出处或资源；直接回答某垂域事实仍归对应垂域。\n"
        f"5. {disp_doc}用于基于给定文件内容的摘要、抽取、比较、改写或问答。\n"
        f"6. {disp_lbs}用于路线、行程、地点、酒店、景点、餐饮和导航规划。\n"
        "7. 同时包含多个意图时，以用户最终希望你交付的核心结果分类。\n\n"
        "对比例子：\n" + "\n".join(fewshot_lines) + "\n\n"
        "在心中完成意图判断后，只输出标签本身，不输出解释、标点或 JSON。"
    )

    # 重试循环
    for attempt in range(max_attempts):
        try:
            logger.info(f"[{item.id}] 分类开始 (attempt {attempt + 1}/{max_attempts})")
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"查询：{item.question}\n\n标签："},
                ],
                temperature=0,
                max_tokens=200,
                extra_body={"enable_thinking": False},
            )

            # 防护：API 可能返回空 choices
            if not resp or not resp.choices:
                logger.warning(f"[{item.id}] 空响应，attempt {attempt + 1}")
                if attempt < max_attempts - 1:
                    wait = min(20.0, 2 ** attempt)
                    logger.info(f"[{item.id}] 等待 {wait:.0f}s 后重试...")
                    await asyncio.sleep(wait)
                    continue
                return None, "(空响应)", attempt + 1

            raw = (resp.choices[0].message.content or "").strip()
            result = _normalize_label(raw, labels, fallback)

            if result:
                logger.info(f"[{item.id}] 分类成功: {raw} -> {result}")
            else:
                logger.info(f"[{item.id}] 回落 default: {raw}")

            return result, raw, attempt + 1

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            retriable = any(
                k in msg
                for k in ("RateLimit", "Overload", "429", "Timeout", "Connection", "ServiceUnavailable")
            )
            if retriable and attempt < max_attempts - 1:
                wait = min(20.0, 2 ** attempt)
                logger.warning(f"[{item.id}] 可重试错误 (attempt {attempt + 1}): {msg}")
                logger.info(f"[{item.id}] 等待 {wait:.0f}s 后重试...")
                await asyncio.sleep(wait)
                continue
            logger.error(f"[{item.id}] 分类失败: {msg}")
            return None, msg, attempt + 1

    return None, "(超过最大重试次数)", max_attempts


# =========================================================================== #
# Excel 生成（参考 history.py build_xlsx）
# =========================================================================== #
def build_xlsx(results: list[dict], summary: dict, router: SkillRouter) -> bytes:
    """生成测试报告 xlsx。"""
    sheets = {
        "测试汇总": [_summary_row(summary)],
        "逐题结果": _result_rows(results, router),
        "垂域详情": _category_detail_rows(summary.get("category_stats", {}), router),
    }

    names = list(sheets)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(sheets)))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook_xml(names))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        zf.writestr("xl/styles.xml", _styles_xml())
        for i, (_name, rows) in enumerate(sheets.items(), start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return buf.getvalue()


def _summary_row(summary: dict) -> dict:
    """汇总行。"""
    return {
        "总数": summary.get("total", 0),
        "正确": summary.get("correct", 0),
        "准确率": f"{summary.get('accuracy', 0):.2%}",
        "测试时间": summary.get("test_time", ""),
        "模型": summary.get("model", ""),
    }


def _result_rows(results: list[dict], router: SkillRouter) -> list[dict]:
    """逐题结果行。"""
    rows = []
    for r in results:
        actual = r.get("actual") or "default"
        expected = r.get("expected") or "default"
        rows.append({
            "序号": r.get("idx", 0),
            "ID": r.get("id", ""),
            "问题": r.get("question", "")[:100],
            "期望分类": expected,
            "期望展示名": router.display_of(expected) if expected != "default" else "通用",
            "实际分类": actual,
            "实际展示名": router.display_of(actual) if actual != "default" else "通用",
            "原始响应": r.get("raw_response", ""),
            "尝试次数": r.get("attempts", 0),
            "结果": "正确" if r.get("correct") else "错误",
        })
    return rows


def _category_detail_rows(category_stats: dict, router: SkillRouter) -> list[dict]:
    """垂域详情行。"""
    rows = []
    for exp, acts in sorted(category_stats.items()):
        total_exp = sum(acts.values())
        correct_exp = acts.get(exp, 0)
        acc = correct_exp / total_exp if total_exp > 0 else 0
        exp_disp = router.display_of(exp) if exp != "default" else "通用"
        rows.append({
            "垂域": exp,
            "展示名": exp_disp,
            "总数": total_exp,
            "正确": correct_exp,
            "准确率": f"{acc:.0%}",
        })
        for act, cnt in sorted(acts.items()):
            if act != exp:
                act_disp = router.display_of(act) if act != "default" else "通用"
                rows.append({
                    "垂域": "",
                    "展示名": f"  → 误判为 {act} ({act_disp})",
                    "总数": "",
                    "正确": cnt,
                    "准确率": "",
                })
    return rows


def _headers(rows: list[dict]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    return keys


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _content_types(sheet_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _workbook_xml(names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(_sheet_name(name))}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\*:/\\?]", "_", name)
    return cleaned[:31] or "Sheet"


def _workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    rels += (
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )


def _sheet_xml(rows: list[dict]) -> str:
    headers = _headers(rows)
    table = [headers] + [[row.get(h) for h in headers] for row in rows]
    rows_xml = []
    for r_idx, row in enumerate(table, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = f"{_col(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            if r_idx > 1 and isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(_cell(value))}</t></is></c>')
        rows_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{_width(h)}" customWidth="1"/>'
        for i, h in enumerate(headers, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{cols}</cols><sheetData>{''.join(rows_xml)}</sheetData>"
        "</worksheet>"
    )


def _col(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _width(header: str) -> int:
    if header in {"问题", "原始响应"}:
        return 50
    if header.startswith("理由_"):
        return 30
    return 15


# =========================================================================== #
# 主测试逻辑
# =========================================================================== #
async def run_classify_test(
    data_path: str,
    config_dir: str | Path | None = None,
    limit: int | None = None,
    output_xlsx: str | None = None,
) -> dict[str, Any]:
    """运行分类测试。

    Args:
        data_path: 测试数据路径
        config_dir: 配置目录（默认使用项目 config/）
        limit: 限制测试数量
        output_xlsx: 输出 xlsx 路径（默认 runs/classify_test_result.xlsx）

    Returns:
        测试结果统计
    """
    # 加载配置
    config_dir = Path(config_dir) if config_dir else ROOT / "config"
    config = load_config(config_dir)
    router = SkillRouter(config.domain_skills)

    # 获取分类配置
    eval_opts = config.eval_options
    base_url = eval_opts.classify_base_url
    api_key_env = eval_opts.classify_api_key_env or "PROXY_API_KEY"
    model = eval_opts.classify_model

    if not base_url:
        if config.judges:
            base_url = config.judges[0].base_url
        else:
            raise ValueError("未配置 classify_base_url，且无裁判配置可回落")

    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"未配置环境变量 {api_key}")

    if not model:
        model = config.judges[0].model if config.judges else "gpt-4o-mini"

    logger.info(f"分类模型: {model}")
    logger.info(f"API 地址: {base_url}")

    # 创建客户端（禁用代理）
    http_client = httpx.AsyncClient(trust_env=False)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client, timeout=30.0)

    # 加载测试数据
    test_data = load_test_data(data_path)
    if limit:
        test_data = test_data[:limit]

    logger.info(f"加载 {len(test_data)} 条测试数据")

    # 显示垂域列表
    labels = _skill_labels(router)
    logger.info(f"垂域标签 ({len(labels)} 个): {', '.join(d for _, d in labels)}")

    # 运行分类
    results: list[dict[str, Any]] = []
    correct = 0
    total = 0
    category_stats: dict[str, dict[str, int]] = {}

    print("\n" + "=" * 80)
    print("垂域分类测试")
    print("=" * 80)

    for i, data in enumerate(test_data, 1):
        item = to_eval_item(data)
        expected = data.get("expected_category") or data.get("category") or "default"

        skill_name, raw_response, attempts = await classify_item(item, client, model, router)
        actual = skill_name or "default"

        # 统计
        total += 1
        if actual == expected:
            correct += 1

        if expected not in category_stats:
            category_stats[expected] = {}
        category_stats[expected][actual] = category_stats[expected].get(actual, 0) + 1

        # 显示结果
        display = router.display_of(actual) if actual != "default" else "通用"
        status = "[OK]" if actual == expected else "[FAIL]"
        print(f"\n[{i}/{total}] {status} (尝试 {attempts} 次)")
        print(f"  问题: {item.question[:60]}...")
        print(f"  期望: {expected} ({router.display_of(expected) if expected != 'default' else '通用'})")
        print(f"  实际: {actual} ({display})")
        print(f"  原始响应: {raw_response[:50] if raw_response else '(空)'}")

        results.append({
            "idx": i,
            "id": item.id,
            "question": item.question,
            "expected": expected,
            "actual": actual,
            "raw_response": raw_response,
            "attempts": attempts,
            "correct": actual == expected,
        })

    # 汇总
    accuracy = correct / total if total > 0 else 0

    print("\n" + "=" * 80)
    print("测试汇总")
    print("=" * 80)
    print(f"总数: {total}")
    print(f"正确: {correct}")
    print(f"准确率: {accuracy:.2%}")

    print("\n各垂域分类详情:")
    for exp, acts in sorted(category_stats.items()):
        exp_disp = router.display_of(exp) if exp != "default" else "通用"
        total_exp = sum(acts.values())
        correct_exp = acts.get(exp, 0)
        acc = correct_exp / total_exp if total_exp > 0 else 0
        print(f"  {exp} ({exp_disp}): {correct_exp}/{total_exp} ({acc:.0%})")
        for act, cnt in sorted(acts.items()):
            if act != exp:
                act_disp = router.display_of(act) if act != "default" else "通用"
                print(f"    -> 误判为 {act} ({act_disp}): {cnt}")

    # 生成汇总数据
    summary = {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "category_stats": category_stats,
        "test_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
    }

    # 输出 Excel
    xlsx_path = Path(output_xlsx) if output_xlsx else ROOT / "runs" / "classify_test_result.xlsx"
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_bytes = build_xlsx(results, summary, router)
    xlsx_path.write_bytes(xlsx_bytes)
    print(f"\nExcel 报告已保存: {xlsx_path}")

    return summary


# =========================================================================== #
# pytest 测试
# =========================================================================== #
import pytest


class TestDomainClassify:
    """垂域分类测试（pytest）。"""

    @pytest.mark.asyncio
    async def test_classify_with_real_api(self):
        """使用真实 API 测试分类。"""
        data_path = ROOT / "data" / "test_skills_with_doubao_all_sample2.jsonl"
        if not data_path.exists():
            pytest.skip(f"测试数据不存在: {data_path}")

        if not os.environ.get("PROXY_API_KEY"):
            pytest.skip("未配置 PROXY_API_KEY 环境变量")

        result = await run_classify_test(str(data_path), limit=10)
        assert result["total"] > 0, "应至少有1条测试数据"


# =========================================================================== #
# 命令行入口
# =========================================================================== #
def main():
    parser = argparse.ArgumentParser(description="垂域分类模块测试")
    parser.add_argument(
        "--data",
        type=str,
        default=str(ROOT / "data" / "test_skills_with_doubao_all_sample2.jsonl"),
        help="测试数据路径 (默认: data/test_skills_with_doubao_all_sample2.jsonl)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置目录 (默认: 项目 config/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制测试数量",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出 xlsx 路径 (默认: runs/classify_test_result.xlsx)",
    )
    args = parser.parse_args()

    # 检查数据文件
    if not Path(args.data).exists():
        print(f"错误: 数据文件不存在: {args.data}")
        print(f"\n请准备测试数据，格式为 jsonl，每行包含:")
        print('  {"question": "问题内容", "expected_category": "期望分类(可选)"}')
        sys.exit(1)

    # 运行测试
    asyncio.run(run_classify_test(
        data_path=args.data,
        config_dir=args.config,
        limit=args.limit,
        output_xlsx=args.output,
    ))


if __name__ == "__main__":
    main()
