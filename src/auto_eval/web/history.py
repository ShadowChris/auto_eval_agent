"""Web 评测历史持久化与完整导出。

这里刻意不用数据库：评测台是本地/轻量服务，JSON 快照足够支撑历史加载；
XLSX 直接生成 OOXML，避免给项目额外引入 openpyxl / xlsxwriter 依赖。
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from ..paths import RUNS_DIR


HISTORY_DIR = RUNS_DIR / "web_history"


def _task_path(task_id: str) -> Path:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", task_id)
    return HISTORY_DIR / f"{safe}.json"


def task_to_snapshot(task) -> dict:
    return {
        "task_id": task.id,
        "mode": task.mode,
        "items": task.items,
        "options": task.options,
        "status": task.status,
        "results": task.results,
        "summary": task.summary,
        "created_at": task.created_at,
        "updated_at": time.time(),
        "done_total": task.done_total,
        "error": task.error,
    }


def save_task(task) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _task_path(task.id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(task_to_snapshot(task), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_snapshot(task_id: str) -> dict | None:
    path = _task_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_snapshot(task_id: str) -> bool:
    """删除某次评测的快照文件。返回是否删除成功（文件存在且已删除）。"""
    path = _task_path(task_id)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        return False


def list_snapshots(limit: int = 50) -> list[dict]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in HISTORY_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = data.get("status")
        error = data.get("error")
        if status in {"pending", "running"}:
            status = "error"
            error = error or "服务中断，已保留中断前完成的评估结果"
        rows.append({
            "task_id": data.get("task_id") or path.stem,
            "mode": data.get("mode"),
            "status": status,
            "total": len(data.get("items") or []),
            "done": len([r for r in (data.get("results") or []) if "error" not in r]),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at") or data.get("created_at"),
            "error": error,
            "preview": _preview(data),
        })
    rows.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return rows[:limit]

def _preview(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return ""
    q = str(items[0].get("query") or "")
    return q[:80] + ("…" if len(q) > 80 else "")


def snapshot_payload(data: dict) -> dict:
    return {
        "task_id": data.get("task_id"),
        "mode": data.get("mode"),
        "items": data.get("items") or [],
        "options": data.get("options") or {},
        "status": data.get("status"),
        "results": data.get("results") or [],
        "summary": data.get("summary") or {},
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "error": data.get("error"),
    }


def export_rows(snapshot: dict) -> dict[str, list[dict]]:
    """把一次评测拆成多个 Sheet 的行数据。

    逐题结果按垂域分 sheet（同垂域维度一致，可展开列做筛选/分析）；
    另保留一个"逐题结果"概览 sheet（维度塞单列，避免跨垂域维度并集导致列爆炸；CSV 导出走它）。
    """
    results = snapshot.get("results") or []
    summary = snapshot.get("summary") or {}
    by_skill = summary.get("by_skill") if isinstance(summary.get("by_skill"), dict) else {}
    overview = by_skill.get("overview") or []
    sections = by_skill.get("sections") or []
    mode = snapshot.get("mode")

    rows: dict[str, list[dict]] = {"运行信息": [_run_info(snapshot)]}
    if mode == "compare":
        rows["逐题结果"] = _result_rows(results)
    else:
        rows["逐题结果"] = _result_rows_compact(results)
        for name, skill_rows in _per_skill_sheets(results):
            rows[name] = skill_rows
    rows["垂域总览"] = overview
    rows["维度问题占比"] = _dim_problem_rows(sections)
    rows["图表数据"] = _chart_rows(summary)
    if summary:
        rows["汇总指标"] = [_flatten_dict(summary, skip_keys={"by_skill"})]
    return rows


def _run_info(snapshot: dict) -> dict:
    created = snapshot.get("created_at")
    updated = snapshot.get("updated_at")
    return {
        "task_id": snapshot.get("task_id"),
        "mode": snapshot.get("mode"),
        "status": snapshot.get("status"),
        "total": len(snapshot.get("items") or []),
        "done": len([r for r in (snapshot.get("results") or []) if "error" not in r]),
        "created_at": _format_ts(created),
        "updated_at": _format_ts(updated),
        "options": snapshot.get("options") or {},
        "error": snapshot.get("error") or "",
    }


def _format_ts(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _result_rows(results: list[dict]) -> list[dict]:
    """维度展开成列（适用于同垂域维度一致的场景，如各垂域分 sheet）；
    每个维度同时输出 分(维度_X) 和 打分理由(理由_X) 两列。"""
    rows = []
    for r in results:
        row = dict(r)
        rubric = row.pop("rubric", {}) or {}
        reasons = row.pop("rubric_reasons", {}) or {}
        for dim, score in rubric.items():
            row[f"维度_{dim}"] = score
            row[f"理由_{dim}"] = reasons.get(dim, "")
        rows.append(row)
    return rows


def _result_rows_compact(results: list[dict]) -> list[dict]:
    """逐题概览：维度分与理由各塞单列（避免跨垂域维度并集导致列爆炸）。"""
    rows = []
    for r in results:
        row = dict(r)
        rubric = row.pop("rubric", {}) or {}
        reasons = row.pop("rubric_reasons", {}) or {}
        row["各维度分"] = "  ".join(f"{k}:{v}" for k, v in rubric.items()) if rubric else ""
        row["各维度理由"] = "  ".join(f"{k}:{reasons[k]}" for k in rubric if k in reasons) if rubric else ""
        rows.append(row)
    return rows


def _per_skill_sheets(results: list[dict]) -> list[tuple[str, list[dict]]]:
    """按垂域分组返回 [(sheet名, 行数据)]；同垂域维度一致可展开列，评估失败的题单独成 sheet。"""
    groups: dict[str, dict] = {}
    failed: list[dict] = []
    for r in results:
        if "error" in r:
            failed.append(r)
            continue
        cat = r.get("category") or "default"
        disp = r.get("category_display") or cat
        groups.setdefault(cat, {"display": disp, "rows": []})["rows"].append(r)
    out = []
    for _cat, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["rows"])):
        out.append((_sheet_name(f"逐题-{g['display']}"), _result_rows(g["rows"])))
    if failed:
        out.append(("评估失败", _result_rows(failed)))
    return out


def _dim_problem_rows(sections: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for section in sections:
        for dim, info in (section.get("dim_problem_dist") or {}).items():
            rows.append({
                "skill": section.get("skill"),
                "垂域": section.get("display"),
                "维度": dim,
                "问题题数": info.get("count", len(info.get("item_ids") or [])),
                "占比": info.get("rate"),
                "样例题号": ", ".join(map(str, info.get("item_ids") or [])),
                "样本量": section.get("n_items"),
            })
    return rows


def _chart_rows(summary: dict) -> list[dict]:
    """Excel 图表专用数据源。

    采用宽表而不是“图表/名称/值”长表，方便 OOXML chart 直接引用连续区域。
    """
    by_skill = summary.get("by_skill") if isinstance(summary.get("by_skill"), dict) else {}
    pie_rows = [
        {"垂域": row.get("display"), "样本量": row.get("n_items")}
        for row in (by_skill.get("overview") or [])
        if row.get("n_items", 0) > 0
    ]
    bar_rows = []
    for section in by_skill.get("sections") or []:
        for dim, info in (section.get("dim_problem_dist") or {}).items():
            if (info.get("rate") or 0) <= 0:
                continue
            bar_rows.append({
                "维度问题": f"{section.get('display')} - {dim}",
                "占比": info.get("rate"),
                "问题题数": info.get("count"),
            })

    rows = []
    for i in range(max(len(pie_rows), len(bar_rows))):
        row = {}
        if i < len(pie_rows):
            row.update(pie_rows[i])
        if i < len(bar_rows):
            row.update(bar_rows[i])
        rows.append(row)
    return rows


def _flatten_dict(data: dict, skip_keys: set[str] | None = None) -> dict:
    skip_keys = skip_keys or set()
    return {k: v for k, v in data.items() if k not in skip_keys}


def rows_to_csv(rows: list[dict]) -> str:
    import csv
    from io import StringIO

    out = StringIO()
    keys = _headers(rows)
    writer = csv.DictWriter(out, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in keys})
    return out.getvalue()


def build_xlsx(snapshot: dict) -> bytes:
    """生成 xlsx（纯数据 sheet，不含图表）。

    手写 OOXML chart 易被 Excel 判"需修复"且样式差，故不再导出图表——
    图表请看 web 端 ECharts；如需在 Excel 画图，用"图表数据"sheet 自行插入。
    """
    sheets = {name: rows for name, rows in export_rows(snapshot).items() if rows}
    if not sheets:
        sheets = {"逐题结果": []}

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



def _sheet_drawing_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
        'Target="../drawings/drawing1.xml"/>'
        '</Relationships>'
    )


def _drawing_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>'
        '</Relationships>'
    )


def _drawing_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'{_chart_anchor("rId1", 0, 1, 9, 19, "垂域样本分布")}'
        f'{_chart_anchor("rId2", 10, 1, 23, 19, "维度问题占比")}'
        '</xdr:wsDr>'
    )


def _chart_anchor(rid: str, col1: int, row1: int, col2: int, row2: int, name: str) -> str:
    return (
        '<xdr:twoCellAnchor>'
        f'<xdr:from><xdr:col>{col1}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
        f'<xdr:to><xdr:col>{col2}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row2}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
        '<xdr:graphicFrame macro="">'
        '<xdr:nvGraphicFramePr>'
        f'<xdr:cNvPr id="{1 if rid == "rId1" else 2}" name="{escape(name)}"/>'
        '<xdr:cNvGraphicFramePr/>'
        '</xdr:nvGraphicFramePr>'
        '<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="{rid}"/>'
        '</a:graphicData></a:graphic>'
        '</xdr:graphicFrame>'
        '<xdr:clientData/>'
        '</xdr:twoCellAnchor>'
    )


def _quoted_sheet(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _doughnut_chart_xml(data_sheet: str, title: str) -> str:
    sh = _quoted_sheet(data_sheet)
    cats = f"{sh}!$A$2:$A$500"
    vals = f"{sh}!$B$2:$B$500"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<c:chart>'
        f'{_chart_title(title)}'
        '<c:plotArea><c:layout/><c:doughnutChart><c:varyColors val="1"/>'
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f'<c:cat><c:strRef><c:f>{escape(cats)}</c:f></c:strRef></c:cat>'
        f'<c:val><c:numRef><c:f>{escape(vals)}</c:f></c:numRef></c:val>'
        '</c:ser><c:firstSliceAng val="270"/><c:holeSize val="55"/></c:doughnutChart></c:plotArea>'
        '<c:legend><c:legendPos val="r"/><c:layout/></c:legend><c:plotVisOnly val="1"/>'
        '</c:chart></c:chartSpace>'
    )


def _bar_chart_xml(data_sheet: str, title: str) -> str:
    sh = _quoted_sheet(data_sheet)
    cats = f"{sh}!$C$2:$C$500"
    vals = f"{sh}!$D$2:$D$500"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<c:chart>'
        f'{_chart_title(title)}'
        '<c:plotArea><c:layout/><c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>'
        '<c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>问题占比</c:v></c:tx>'
        f'<c:cat><c:strRef><c:f>{escape(cats)}</c:f></c:strRef></c:cat>'
        f'<c:val><c:numRef><c:f>{escape(vals)}</c:f></c:numRef></c:val>'
        '</c:ser><c:axId val="123456"/><c:axId val="123457"/></c:barChart>'
        '<c:catAx><c:axId val="123456"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:axPos val="b"/><c:tickLblPos val="nextTo"/><c:crossAx val="123457"/><c:crosses val="autoZero"/></c:catAx>'
        '<c:valAx><c:axId val="123457"/><c:scaling><c:orientation val="minMax"/><c:max val="1"/><c:min val="0"/></c:scaling>'
        '<c:axPos val="l"/><c:numFmt formatCode="0%" sourceLinked="0"/><c:majorGridlines/><c:tickLblPos val="nextTo"/>'
        '<c:crossAx val="123456"/><c:crosses val="autoZero"/></c:valAx>'
        '</c:plotArea><c:legend><c:legendPos val="b"/><c:layout/></c:legend><c:plotVisOnly val="1"/>'
        '</c:chart></c:chartSpace>'
    )


def _chart_title(title: str) -> str:
    return (
        '<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN"/>'
        f'<a:t>{escape(title)}</a:t>'
        '</a:r></a:p></c:rich></c:tx><c:layout/></c:title>'
    )


def _col(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _width(header: str) -> int:
    if header in {"query", "answer", "generated_answer", "rationale", "理由", "options"}:
        return 42
    if header in {"各维度分", "各维度理由"}:
        return 36
    if header.startswith("理由_"):
        return 30
    if header.startswith("维度_"):
        return 14
    return 18
