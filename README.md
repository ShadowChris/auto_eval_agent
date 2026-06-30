# auto-eval-agent · 自动评估与优化建议 Agent

> 📖 **完整项目文档（推荐先读）**：[docs/项目文档.md](docs/项目文档.md) —— 详解盲评 agent loop、深度推演、事实核查协议、多裁判集成、成对比较、元评测等，看完即懂项目全貌。

盲评 + 多裁判 + 元评测的 LLM 评测框架。支持批量批跑待评测 agent 与竞品（如豆包），对**混合评测集**（有/无参考答案）做盲评，并用参考答案在最后**校验评测 agent 本身**的可靠性。

## 三条核心设计原则

1. **盲评**：被测模型只拿 `question+context`（绝不传参考答案）；裁判也不接收参考答案，靠 rubric + 多裁判 + 联网检索独立判断。
2. **参考答案做元评测**：参考答案最后才出场，作为 ground truth 校验"评测 agent 本身准不准"（不是测被测模型）。
3. **无参考答案题：模拟人工评测 + 补足人工缺点**——像一群不同背景的真人评测员那样评开放题，并用机制克服人工评测的固有问题（主观不一致、疲劳、知识盲区、位置/锚定偏差、规模小、不可复现）。

## 数据流

```
评测集 → [批跑] 被测模型盲答 → [盲评] 多裁判独立评判(可联网)
       → [元评测] 用参考答案校验评测 agent → [分析] 对比/胜率/弱点 → [报告] A对比 + B可靠性
```

---

## 安装

```powershell
# 1. 安装依赖（Python 3.10+；web 组提供前端评估台）
pip install -e ".[dev,web]"

# 2. 配置密钥
cp .env.example .env                 # 填入 PROXY_API_KEY / TAVILY_API_KEY 等

# 3. 编辑配置（按你的模型/裁判）
#    config/models.yaml    被测模型（my_agent + 竞品）
#    config/judges.yaml    裁判（model / persona / 启用哪些工具）
#    config/rubrics.yaml   评测维度与权重
#    data/dataset.jsonl    你的评测集（示例 schema 见 data/）
```

> ⚠️ **环境注意**：本机若 `python`（hermes venv）与 `pip`（anaconda）不一致，下面所有启动命令请用 `& "D:\ProgramData\anaconda3\python.exe" -m ...`，否则报 `ModuleNotFoundError`。

---

## 启动与终止

> 本项目**前后端一体**：FastAPI 后端同时托管前端页面（[web/static](src/auto_eval/web/static/)，Vue3 CDN、**无需构建**）。**启动 web 服务 = 前后端都起来了**，浏览器访问即可，没有独立的前端启动步骤。

### 方式 A：Web 评估台（推荐，有界面）

**启动**——在独立 PowerShell 终端运行，**保持窗口开着**：

```powershell
cd auto_eval_agent
$env:PYTHONPATH="D:\workspace\quick_test\auto_eval_agent\src"
python -m uvicorn auto_eval.web.server:app --host 0.0.0.0 --port 8502
```

看到 `Uvicorn running on http://0.0.0.0:8501` 后，浏览器打开 **http://localhost:8501** 。

界面支持三种模式（Tab 切换）：单回答盲评 / 两回答对比 / 接模型在线评估；粘贴（`|||` 分隔）或上传 jsonl 批量输入，SSE 实时出结果，可导出 CSV/JSON。

**终止**：在该终端按 `Ctrl+C`。

> 端口被占（进程没清干净）时强制清理 8502：
> ```powershell
> Get-NetTCPConnection -LocalPort 8502 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
> ```

### 方式 B：CLI（无界面，命令行一条龙）

```powershell
# 小批量先跑通
& "D:\ProgramData\anaconda3\python.exe" -m auto_eval.cli run -d data/dataset.jsonl --limit 20
# 全量
& "D:\ProgramData\anaconda3\python.exe" -m auto_eval.cli run -d data/dataset.jsonl
```

其他子命令：`eval-only`（复用已有答案只评测）、`list`（列历史运行）。CLI 跑完自动结束，无需单独终止。

产物在 `runs/<run_id>/`：`answers/`（盲答）、`verdicts/`（盲评）、`meta/`（元评测）、`reports/`（A 对比报告 + B 可靠性报告，各含 md/json）。

### 裁判明细日志（可选）

在 `.env` 设 `AUTO_EVAL_JUDGE_TRACE=auto_eval_agent/runs/judge_calls.jsonl`，每次裁判评判的完整明细（每轮 LLM 响应、工具完整返回、对话历史）会追加到该文件，便于调试/审计。**不设则不记录、零开销**。详见 [项目文档 §9 Q7](docs/项目文档.md)。

---

## 目录

核心模块（完整说明见 [docs/项目文档.md](docs/项目文档.md)）：

- `schema.py` 数据模型｜`dataset.py` 数据加载 + reference 隔离
- `runners/` 可插拔模型适配（openai_compat / http / func / cli）
- `batch/` 异步批跑 + 限流 + 重试 + 断点续跑
- `judges/` 盲评引擎（rubric / pairwise / 多裁判集成 / 联网工具 / agent loop）
- `meta/` 元评测（客观真值 + 裁判校准）
- `analysis/` 聚合 / 对比 / case 挖掘 / 优化建议
- `report/` A 对比报告 + B 可靠性报告
- `cli.py` 命令行入口｜`web/` 前端评估台（FastAPI + Vue3）

## 需要你补充的输入

1. 评测集样本（确认 `EvalItem` 字段对齐）；
2. 模型接入 key（代理 / 火山 ARK / Moonshot 等，按 `models.yaml` + `judges.yaml` 的 `api_key_env`）；
3. 自研 agent 的调用方式（HTTP / Python 函数 / 命令行 → 选对应 runner）；
4. 联网检索服务（Tavily / SerpAPI / Bing 三选一）；
5. 裁判模型须支持 function calling（glm-5.2 / glm-4.7 / Moonshot kimi 等可用；注意某些代理上的 deepseek-v4 会因 tools 格式报错，详见 [项目文档 §9 Q2](docs/项目文档.md)）。
