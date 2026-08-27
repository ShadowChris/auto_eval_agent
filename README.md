# auto-eval-agent · 垂域视觉评测 Web 评估台

> 📖 模式详解见 [docs/垂域视觉评测.md](docs/垂域视觉评测.md)；[docs/项目文档.md](docs/项目文档.md) 是早期通用评测框架（盲评/多裁判/元评测）的历史文档，仅作背景参考，其中多数能力已下线。

面向**录屏答题视频**的 LLM 评测工具，只保留两种评测模式、一位裁判：

| 模式 | 说明 | 裁判 |
| --- | --- | --- |
| **垂域视觉评测**（`rich_content`） | 识别回答中的挂卡（天气/音乐等垂域卡片）与 Superlink，统计数量、判定适用性，标记需人工复核的条目 | 终端用户（`judge_2`） |
| **垂域视觉对比评测**（`compare`） | 同一问题的两个回答视频，五维对比（相关性/安全/内容质量/需求闭环/个性化）+ 内容冲突检测 | 终端用户（`judge_2`） |

裁判「终端用户」为单轮直出的多模态模型：`Qwen/Qwen3.5-397B-A17B`（SiliconFlow），`temperature=0 + seed=42` 保证复跑一致；一次生成 `<analysis>` 思考链 + 结论 JSON，不联网、不调工具。

## 数据流

```
JSONL（含 video_path / video1+video2）→ [抽帧] 场景切分 + 关键帧提取
    → [裁判] 单轮多模态直出逐题评判 → [SSE] 前端实时出结果
    → [导出] XLSX / 抽帧 zip / 单题 judge_calls JSON
```

---

## 安装

```powershell
# 1. 安装依赖（Python 3.10+；web 组提供评估台前后端）
pip install -e ".[dev,web]"

# 2. 配置密钥：项目根目录建 .env，至少填入
#    SILICON_FLOW_API_KEY=...   # 裁判模型

# 3. 按需调整配置
#    config/judges.yaml               裁判模型（单轮直出，不联网不调工具）
#    config/visual_modes/rich_content.yaml   抽帧算法参数 + 垂域显示名映射（两种模式共用）
```

> ⚠️ **环境注意**：本机若 `python`（hermes venv）与 `pip`（anaconda）不一致，下面所有启动命令请用 `& "D:\ProgramData\anaconda3\python.exe" -m ...`，否则报 `ModuleNotFoundError`。

---

## 启动与终止

> 本项目**前后端一体**：FastAPI 后端同时托管前端页面（[web/static](src/auto_eval/web/static/)，Vue3 CDN、**无需构建**）。**启动 web 服务 = 前后端都起来了**，浏览器访问即可，没有独立的前端启动步骤。

**启动**——在独立 PowerShell 终端运行，**保持窗口开着**：

```powershell
cd auto_eval_agent-多轮稳定版
$env:PYTHONPATH=".\src"
python -m uvicorn auto_eval.web.server:app --host 0.0.0.0 --port 8503
```

看到 `Uvicorn running on http://0.0.0.0:8503` 后，浏览器打开 **http://localhost:8503** 。

界面操作：选择评测模式 → 导入 JSONL（多轮会话可导入 CSV，按 `session_group` 串行、`turn_index` 排序）→ 选择裁判与并发数 → 开始评测，SSE 实时出结果，完成后可导出。

**终止**：在该终端按 `Ctrl+C`。

> 端口被占（进程没清干净）时强制清理 8503：
> ```powershell
> Get-NetTCPConnection -LocalPort 8503 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
> ```

### 输入格式（JSONL，每行一题）

- **垂域视觉评测**：`query`（必填）、`video_path`（必填）、可选 `id` / `context`（可信背景条件，可写当前时间地点）/ `category`（垂域）/ `answer_text` / `task_start_time`+`task_end_time`（截取作答区间）。
- **垂域视觉对比评测**：`query`、`video1`+`video2`（必填）、`answer1`+`answer2`、可选 `context` / `context1` / `context2`。

### 裁判明细日志（可选）

在 `.env` 设 `AUTO_EVAL_JUDGE_TRACE=runs/judge_calls.jsonl`，每次裁判评判的完整明细（LLM 响应、对话历史）会追加到该文件，便于调试/审计；前端也可按题导出 judge_calls。**不设则不记录、零开销**。

---

## 目录

- `schema.py` 数据模型（EvalItem / RichContentObservation / VisualCompareObservation）
- `judges/` 评测引擎：`base.py`（单轮直出 LLM 客户端 + JSON 定向修复）｜`rich_content_judge.py`｜`visual_compare_judge.py`｜`prompts.py`（两套 SYSTEM/USER 模板）
- `web/` 评估台：`server.py`（FastAPI 路由）｜`runner.py`（评测编排：抽帧→逐题评判→汇总）｜`parse_input.py`（JSONL/CSV 解析）｜`video_prepare.py` + `media.py`（抽帧）｜`history.py`（快照与导出）｜`static/`（前端）
- `config/` `judges.yaml`｜`visual_modes/rich_content.yaml`（含 category_display 垂域显示名）
- `tests/` pytest 单元测试（不访问真实模型/网络）

## 需要你补充的输入

1. 录屏视频文件（JSONL 中写相对/绝对路径，上传目录也可通过界面上传）；
2. `SILICON_FLOW_API_KEY`（裁判模型）。
