"""命令行入口。

用法：
  auto-eval run --dataset data/dataset.jsonl --limit 20
  auto-eval eval-only --run-dir runs/run-20240101-120000 --dataset data/dataset.jsonl
  auto-eval list
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台默认 GBK
except Exception:
    pass

import typer
from dotenv import load_dotenv

from .batch.checkpoint import AnswerStore
from .batch.orchestrator import Orchestrator, default_progress
from .config import load_config
from .dataset import load_dataset
from .engine import EvalEngine
from .report import build_reports

app = typer.Typer(add_completion=False, help="自动评估与优化建议 Agent")


def _eval_progress(done: int, total: int):
    if total == 0:
        return
    sys.stdout.write(f"\r盲评/比较 [{done}/{total}] {done * 100 // total}%   ")
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")


def _write_reports(run_dir: Path, results, items, ans_index, cfg, run_id):
    reports = build_reports(results, items, ans_index, cfg, run_id)
    rep = run_dir / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "A.md").write_text(reports["A_md"], encoding="utf-8")
    (rep / "A.json").write_text(
        json.dumps(reports["A"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rep / "B.md").write_text(reports["B_md"], encoding="utf-8")
    (rep / "B.json").write_text(
        json.dumps(reports["B"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rep / "C.md").write_text(reports["C_md"], encoding="utf-8")
    (rep / "C.json").write_text(
        json.dumps(reports["C"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rep / "cases_detail.csv").write_text(reports["cases_csv"], encoding="utf-8-sig")
    return rep


@app.command()
def run(
    dataset: str = typer.Option(..., "--dataset", "-d"),
    config: str = typer.Option("config", "--config", "-c"),
    runs_dir: str = typer.Option("runs", "--runs-dir"),
    limit: int = typer.Option(None, "--limit", help="只跑前 N 题"),
    focal: str = typer.Option(None, "--focal", help="待评测模型名（默认 models.yaml 第一个）"),
):
    """批跑 + 盲评 + 元评测 + 报告，一条龙。"""
    load_dotenv()
    cfg = load_config(config)
    items = load_dataset(dataset, limit=limit)
    run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"▶ 启动 {run_id}：{len(items)} 题，模型 {cfg.model_names()}，裁判 {cfg.judge_names()}")

    # ① 批跑
    typer.echo("▶ ① 批跑被测模型作答 …")
    orch = Orchestrator(run_dir)
    answers_by_model = asyncio.run(orch.run(items, cfg.models, on_progress=default_progress))

    # ② 盲评 + 元评测
    typer.echo("▶ ② 盲评 + 元评测 …")
    engine = EvalEngine(cfg, run_dir, on_progress=_eval_progress)
    results = asyncio.run(engine.evaluate(items, answers_by_model, focal_model=focal))

    # ③ 报告
    typer.echo("▶ ③ 生成报告 …")
    ans_index = {m: {o.item_id: o for o in outs} for m, outs in answers_by_model.items()}
    rep = _write_reports(run_dir, results, items, ans_index, cfg, run_id)
    typer.echo(f"\n✅ 完成。报告：\n  {rep / 'A.md'}（被测对比）\n  {rep / 'B.md'}（评测可靠性）\n  {rep / 'C.md'}（分垂域）\n  {rep / 'cases_detail.csv'}（逐条诊断）")


@app.command(name="eval-only")
def eval_only(
    run_dir: str = typer.Option(..., "--run-dir"),
    dataset: str = typer.Option(..., "--dataset", "-d"),
    config: str = typer.Option("config", "--config", "-c"),
    focal: str = typer.Option(None, "--focal"),
):
    """复用已有答案（断点/历史 run），只跑盲评 + 元评测 + 报告。"""
    load_dotenv()
    cfg = load_config(config)
    items = load_dataset(dataset)
    run_dir = Path(run_dir)
    answers_by_model = {m.name: AnswerStore(run_dir, m.name).load_all() for m in cfg.models}
    total = sum(len(v) for v in answers_by_model.values())
    typer.echo(f"▶ 加载已有答案 {total} 条，开始盲评 …")
    engine = EvalEngine(cfg, run_dir, on_progress=_eval_progress)
    results = asyncio.run(engine.evaluate(items, answers_by_model, focal_model=focal))
    ans_index = {m: {o.item_id: o for o in outs} for m, outs in answers_by_model.items()}
    rep = _write_reports(run_dir, results, items, ans_index, cfg, run_dir.name)
    typer.echo(f"\n✅ 完成。报告：{rep / 'A.md'} / {rep / 'B.md'} / {rep / 'C.md'} / {rep / 'cases_detail.csv'}")


@app.command()
def list_runs(runs_dir: str = typer.Option("runs", "--runs-dir")):
    """列出已有运行。"""
    rd = Path(runs_dir)
    if not rd.exists():
        typer.echo("（暂无运行）")
        return
    for p in sorted(rd.iterdir()):
        if p.is_dir():
            n_ans = sum(1 for _ in (p / "answers").glob("*.jsonl")) if (p / "answers").exists() else 0
            typer.echo(f"  {p.name}  答案文件={n_ans}")


if __name__ == "__main__":
    app()
