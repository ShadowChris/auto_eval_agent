"""轻量化垂域分类测试脚本。

只做垂域分类，不跑完整评测流程。用于快速验证分类效果。

用法：
    # 交互模式：直接输入问题
    python tests/test_classify_lite.py

    # 从文件读取问题（每行一个问题）
    python tests/test_classify_lite.py --file questions.txt

    # 单个问题快速测试
    python tests/test_classify_lite.py --query "2026年1月3号苏州95油价"

    # 输出到文件
    python tests/test_classify_lite.py --file questions.txt --output result.xlsx
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
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

# 加载环境变量
load_dotenv(ROOT / ".env", override=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("classify_lite")


# =========================================================================== #
# 分类核心逻辑（与 rubric_judge._classify 一致）
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


# 重试等待时间（秒）
_RETRY_WAITS = [1, 2, 4, 6, 8]
_MAX_ATTEMPTS = 5


async def classify_query(
    query: str,
    client: AsyncOpenAI,
    model: str,
    skill_router: SkillRouter,
    context: str | None = None,
) -> tuple[str | None, str, int]:
    """对单个 query 进行垂域分类。

    Args:
        query: 用户问题
        client: OpenAI 客户端
        model: 模型名
        skill_router: Skill 路由器
        context: 可选背景信息

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
    disp_math = name_to_disp.get("math_solving", "数学解题")

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
        f"7. 星期、日期、节假日等日历事实默认归{fallback}；只有明确要求日期计算、间隔计算、日历推导或展示计算过程时才归{disp_math}。\n"
        "8. 同时包含多个意图时，以用户最终希望你交付的核心结果分类。\n\n"
        "对比例子：\n" + "\n".join(fewshot_lines) + "\n\n"
        "在心中完成意图判断后，只输出标签本身，不输出解释、标点或 JSON。"
    )

    # 构建 query 文本
    query_text = f"查询：{query}"
    if context:
        query_text += (
            "\n可信背景条件（由评测样本提供，请作为意图判断前提）："
            f"\n{context}"
        )

    # 重试循环
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{query_text}\n\n标签："},
                ],
                temperature=0,
                max_tokens=200,
                extra_body={"enable_thinking": False},
            )

            # 防护：API 可能返回空 choices
            if not resp or not resp.choices:
                logger.warning(f"空响应，attempt {attempt + 1}/{_MAX_ATTEMPTS}")
                if attempt < _MAX_ATTEMPTS - 1:
                    wait = _RETRY_WAITS[attempt]
                    logger.info(f"等待 {wait}s 后重试...")
                    await asyncio.sleep(wait)
                    continue
                return None, "(空响应)", attempt + 1

            raw = (resp.choices[0].message.content or "").strip()
            result = _normalize_label(raw, labels, fallback)
            return result, raw, attempt + 1

        except Exception as e:
            logger.warning(f"请求失败 (attempt {attempt + 1}/{_MAX_ATTEMPTS}): {e}")
            if attempt < _MAX_ATTEMPTS - 1:
                wait = _RETRY_WAITS[attempt]
                logger.info(f"等待 {wait}s 后重试...")
                await asyncio.sleep(wait)
                continue
            return None, f"(错误: {e})", attempt + 1

    return None, "(超过最大重试次数)", _MAX_ATTEMPTS


# =========================================================================== #
# Excel 生成
# =========================================================================== #
def build_xlsx(results: list[dict], router: SkillRouter) -> bytes:
    """生成结果 xlsx。"""
    headers = ["序号", "问题", "分类", "展示名", "原始响应", "尝试次数"]
    rows = [headers]
    for r in results:
        skill = r.get("skill") or "default"
        disp = router.display_of(skill) if skill != "default" else "通用"
        rows.append([
            r.get("idx", 0),
            r.get("query", "")[:100],
            skill,
            disp,
            r.get("raw", ""),
            r.get("attempts", 1),
        ])
    return _to_xlsx(rows)


def _to_xlsx(rows: list[list]) -> bytes:
    """将二维数组转为 xlsx bytes。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types())
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook_xml())
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels())
        zf.writestr("xl/styles.xml", _styles_xml())
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
    return buf.getvalue()


def _content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )


def _workbook_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="分类结果" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )


def _workbook_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
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


def _sheet_xml(rows: list[list]) -> str:
    rows_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = f"{_col(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            if r_idx > 1 and isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(str(value))}</t></is></c>')
        rows_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{20 if i == 2 else 15}" customWidth="1"/>'
        for i in range(1, len(rows[0]) + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{cols}</cols><sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )


def _col(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


# =========================================================================== #
# 主逻辑
# =========================================================================== #
async def run_classify(
    queries: list[str],
    config_dir: str | Path | None = None,
    output_xlsx: str | None = None,
) -> list[dict]:
    """运行分类。

    Args:
        queries: 问题列表
        config_dir: 配置目录
        output_xlsx: 输出 xlsx 路径

    Returns:
        分类结果列表
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
        raise ValueError(f"未配置环境变量 {api_key_env}")

    if not model:
        model = config.judges[0].model if config.judges else "gpt-4o-mini"

    logger.info(f"分类模型: {model}")
    logger.info(f"API 地址: {base_url}")

    # 创建客户端
    http_client = httpx.AsyncClient(trust_env=False)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client, timeout=30.0)

    # 显示垂域列表
    labels = _skill_labels(router)
    logger.info(f"垂域标签 ({len(labels)} 个): {', '.join(d for _, d in labels)}")

    # 运行分类
    results: list[dict] = []

    print("\n" + "=" * 80)
    print("垂域分类测试")
    print("=" * 80)

    for i, query in enumerate(queries, 1):
        skill, raw, attempts = await classify_query(query, client, model, router)
        display = router.display_of(skill) if skill else "通用"

        print(f"\n[{i}/{len(queries)}] (尝试 {attempts} 次)")
        print(f"  问题: {query[:60]}{'...' if len(query) > 60 else ''}")
        print(f"  分类: {skill or 'default'} ({display})")
        print(f"  原始: {raw[:50] if raw else '(空)'}")

        results.append({
            "idx": i,
            "query": query,
            "skill": skill or "default",
            "display": display,
            "raw": raw,
            "attempts": attempts,
        })

    # 输出 Excel
    if output_xlsx:
        xlsx_path = Path(output_xlsx)
    else:
        xlsx_path = ROOT / "runs" / f"classify_lite_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_bytes = build_xlsx(results, router)
    xlsx_path.write_bytes(xlsx_bytes)
    print(f"\n结果已保存: {xlsx_path}")

    return results


# =========================================================================== #
# 命令行入口
# =========================================================================== #
def main():
    parser = argparse.ArgumentParser(description="轻量化垂域分类测试")
    parser.add_argument(
        "--query", "-q",
        type=str,
        help="单个问题快速测试",
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        help="从文件读取问题（每行一个问题）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置目录 (默认: 项目 config/)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出 xlsx 路径",
    )
    args = parser.parse_args()

    # 收集问题
    queries: list[str] = []

    if args.query:
        queries.append(args.query)

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"错误: 文件不存在: {file_path}")
            sys.exit(1)
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    queries.append(line)

    if not queries:
        # 交互模式
        print("请输入问题（空行结束）:")
        while True:
            try:
                line = input().strip()
                if not line:
                    break
                queries.append(line)
            except EOFError:
                break

    if not queries:
        print("未输入任何问题")
        sys.exit(0)

    print(f"共 {len(queries)} 个问题")

    # 运行
    asyncio.run(run_classify(
        queries=queries,
        config_dir=args.config,
        output_xlsx=args.output,
    ))


if __name__ == "__main__":
    main()
