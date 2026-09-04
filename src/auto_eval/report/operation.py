"""离线任务类 HTML 报告：内嵌受控数据和本地资源，展示与 Web 共用。"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


OPERATION_REPORT_ASSETS = Path(__file__).with_name("assets")


def build_operation_report_html(payload: dict[str, Any]) -> bytes:
    """不加载 CDN、不请求 API、不嵌入媒体；打开录屏站点链接时才需要联网。"""
    title = (
        "任务类对比分析报告"
        if payload.get("kind") == "comparison" else "任务类评估报告"
    )
    serialized = (
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )
    css = (OPERATION_REPORT_ASSETS / "operation_report.css").read_text(encoding="utf-8")
    js = (OPERATION_REPORT_ASSETS / "operation_report.js").read_text(encoding="utf-8")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)}</title>
<style>body{{margin:0;padding:24px;background:#f8fafc;font-family:system-ui,sans-serif}}main{{max-width:1180px;margin:auto}}h1{{font-size:22px;color:#1e3a8a}}@media(max-width:600px){{body{{padding:10px}}}}{css}</style>
</head><body><main><h1>{html.escape(title)}</h1>
<div id="operation-report"></div>
<noscript>此交互报告需要启用 JavaScript。</noscript>
</main><script id="operation-report-data" type="application/json">{serialized}</script>
<script>{js}</script><script>
AutoEvalOperationReport.mount(document.getElementById("operation-report"),
JSON.parse(document.getElementById("operation-report-data").textContent));
</script></body></html>"""
    return document.encode("utf-8")
