"""主编排引擎：批跑答案 → 盲评（rubric + pairwise）→ 元评测。

并发策略：全局信号量控制裁判调用总并发，每裁判再用各自 RateLimiter；
每条 Verdict/PairResult 落盘即追加，支持断点续跑；meta 本地计算后整体覆盖写。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .batch.ratelimit import RateLimiter
from .config import AppConfig
from .judges import Arbitrator, JudgeClient, PairwiseJudge, RubricJudge, SkillRouter, aggregate_pairs, aggregate_scores
from .meta import per_item
from .schema import EvalItem, MetaResult, ModelOutput, PairResult, SinglePair, SingleScore, Verdict


@dataclass
class EvalResults:
    verdicts: dict[tuple[str, str], Verdict] = field(default_factory=dict)
    pairs: dict[tuple[str, str, str], PairResult] = field(default_factory=dict)
    metas: list[MetaResult] = field(default_factory=list)
    focal_model: str | None = None


class EvalEngine:
    def __init__(
        self,
        cfg: AppConfig,
        run_dir: str | Path,
        global_concurrency: int = 32,
        on_progress: Callable[[int, int], None] | None = None,
    ):
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.opts = cfg.eval_options
        self.ens = cfg.ensemble
        self.threshold = cfg.ensemble.flag_low_agreement
        self.on_progress = on_progress

        self.verdicts_file = self.run_dir / "verdicts.jsonl"
        self.pairs_file = self.run_dir / "pairs.jsonl"
        self.meta_file = self.run_dir / "meta.jsonl"

        _providers = cfg.eval_options.effective_providers()
        self.clients = [
            JudgeClient(j, _providers, cfg.eval_options.search_topk)
            for j in cfg.judges
        ]
        self.skill_router = SkillRouter(cfg.domain_skills) if cfg.domain_skills else None
        self.rubric_judges = [RubricJudge(c, cfg.rubrics, self.skill_router) for c in self.clients]
        self.pairwise_judges = [PairwiseJudge(c) for c in self.clients]
        self.limiters = {j.name: RateLimiter(j.concurrency) for j in cfg.judges}
        self.global_sem = asyncio.Semaphore(global_concurrency)
        self.scale = cfg.rubrics[0].scale if cfg.rubrics else 5

    # ------------------------------------------------------------------ #
    # 盲评：rubric
    # ------------------------------------------------------------------ #
    async def _score_guarded(self, rj, item, model, answer, k, lim) -> SingleScore | None:
        async with self.global_sem:
            await lim.acquire()
            try:
                return await rj.score(item, model, answer, run_idx=k)
            except Exception:
                return None
            finally:
                lim.release()

    async def _rubric_one(self, item: EvalItem, model: str, answer: str) -> Verdict | None:
        subs = []
        for j_idx, rj in enumerate(self.rubric_judges):
            lim = self.limiters[self.cfg.judges[j_idx].name]
            for k in range(max(1, self.opts.repeat)):
                subs.append(self._score_guarded(rj, item, model, answer, k, lim))
        raw = await asyncio.gather(*subs, return_exceptions=True)
        scores = [r for r in raw if isinstance(r, SingleScore)]
        v = aggregate_scores(scores, self.cfg.rubrics, self.ens, self.threshold)
        # 多裁判分歧 → 主席仲裁（主席看全理由 + 自主联网核查）
        if v and v.low_agreement and len(scores) >= 2:
            v = await self._arbitrate(item, answer, v, scores)
        return v

    async def _arbitrate(self, item: EvalItem, answer: str, v: Verdict, scores: list) -> Verdict:
        arbitrator = Arbitrator(self.clients[0])
        try:
            arb = await arbitrator.arbitrate(item, answer, scores)
        except Exception:
            return v  # 仲裁失败保留原聚合结论
        v.correctness = arb["correctness"]
        v.total = arb["total"]
        v.rubric = arb["rubric"]
        v.arbitrated = True
        v.arbitrator_confidence = arb["confidence"]
        v.arbitrator_rationale = arb["rationale"]
        v.rationale = f"[主席仲裁·置信度{arb['confidence']}] {arb['rationale']}"
        return v

    # ------------------------------------------------------------------ #
    # 盲评：pairwise（focal vs each other）
    # ------------------------------------------------------------------ #
    async def _pair_guarded(self, pj, item, ma, aa, mb, ab, k, order, lim) -> SinglePair | None:
        async with self.global_sem:
            await lim.acquire()
            try:
                return await pj.compare_once(item, ma, aa, mb, ab, run_idx=k, order=order)
            except Exception:
                return None
            finally:
                lim.release()

    async def _pair_one_round(self, item, ma, aa, mb, ab) -> PairResult | None:
        subs = []
        for j_idx, pj in enumerate(self.pairwise_judges):
            lim = self.limiters[self.cfg.judges[j_idx].name]
            if self.opts.pairwise_bidirectional:
                subs.append(self._pair_guarded(pj, item, ma, aa, mb, ab, 0, "ab", lim))
                subs.append(self._pair_guarded(pj, item, ma, aa, mb, ab, 1, "ba", lim))
            else:
                for k in range(max(1, self.opts.repeat)):
                    subs.append(self._pair_guarded(pj, item, ma, aa, mb, ab, k, "ab", lim))
        raw = await asyncio.gather(*subs, return_exceptions=True)
        pairs = [r for r in raw if isinstance(r, SinglePair)]
        return aggregate_pairs(pairs, self.ens, self.threshold)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    async def evaluate(
        self,
        items: list[EvalItem],
        answers_by_model: dict[str, list[ModelOutput]],
        focal_model: str | None = None,
    ) -> EvalResults:
        focal = focal_model or self.cfg.model_names()[0]
        models = self.cfg.model_names()
        ans_index = {m: {o.item_id: o for o in outs} for m, outs in answers_by_model.items()}
        item_map = {it.id: it for it in items}

        verdicts = self._load_verdicts()
        pairs_map = self._load_pairs()

        # rubric 任务
        rubric_jobs = []
        for item in items:
            for model in models:
                out = ans_index.get(model, {}).get(item.id)
                if not out or not out.ok:
                    continue
                if (item.id, model) in verdicts:
                    continue
                rubric_jobs.append((item, model, out.answer))

        # pairwise 任务：focal vs 每个竞品（无 ref 必做；有 ref 看 pairwise_for_ref）
        others = [m for m in models if m != focal]
        pair_jobs = []
        for item in items:
            fa = ans_index.get(focal, {}).get(item.id)
            if not fa or not fa.ok:
                continue
            if item.has_ref and not self.opts.pairwise_for_ref:
                continue
            for other in others:
                oa = ans_index.get(other, {}).get(item.id)
                if not oa or not oa.ok:
                    continue
                if (item.id, focal, other) in pairs_map:
                    continue
                pair_jobs.append((item, focal, fa.answer, other, oa.answer))

        async def do_rubric(item, model, answer):
            v = await self._rubric_one(item, model, answer)
            if v:
                verdicts[(item.id, model)] = v
                self._append(self.verdicts_file, v)

        async def do_pair(item, ma, aa, mb, ab):
            pr = await self._pair_one_round(item, ma, aa, mb, ab)
            if pr:
                pairs_map[(item.id, ma, mb)] = pr
                self._append(self.pairs_file, pr)

        tasks = [do_rubric(*j) for j in rubric_jobs] + [do_pair(*j) for j in pair_jobs]
        total, done = len(tasks), 0

        async def track(coro):
            nonlocal done
            await coro
            done += 1
            if self.on_progress:
                self.on_progress(done, total)

        if tasks:
            await asyncio.gather(*[track(t) for t in tasks])

        # 元评测：本地计算，覆盖写 meta.jsonl
        metas: list[MetaResult] = []
        for (iid, model), v in verdicts.items():
            item = item_map.get(iid)
            if not item:
                continue
            out = ans_index.get(model, {}).get(iid)
            pred = out.answer if out else ""
            metas.append(per_item(v, item, pred, self.scale))
        self._write_all(self.meta_file, metas)

        return EvalResults(verdicts=verdicts, pairs=pairs_map, metas=metas, focal_model=focal)

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _append(self, path: Path, obj: Any) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(obj.model_dump_json() + "\n")

    def _write_all(self, path: Path, objs: list[Any]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for o in objs:
                f.write(o.model_dump_json() + "\n")

    def _load_verdicts(self) -> dict[tuple[str, str], Verdict]:
        out: dict[tuple[str, str], Verdict] = {}
        if not self.verdicts_file.exists():
            return out
        with self.verdicts_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    v = Verdict(**json.loads(line))
                    out[(v.item_id, v.model)] = v
                except Exception:
                    continue
        return out

    def _load_pairs(self) -> dict[tuple[str, str, str], PairResult]:
        out: dict[tuple[str, str, str], PairResult] = {}
        if not self.pairs_file.exists():
            return out
        with self.pairs_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = PairResult(**json.loads(line))
                    out[(p.item_id, p.model_a, p.model_b)] = p
                except Exception:
                    continue
        return out
