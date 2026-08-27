# Repository Guidelines

## Project Structure & Module Organization

Application code is in `src/auto_eval/`. Keep domain models in `schema.py` and configuration in `config.py`/`config/*.yaml`; the project supports two evaluation modes only (`rich_content` 垂域视觉评测, `compare` 垂域视觉对比评测) with a single judge (终端用户, `judge_2`, single-shot — no tools, no web search). Evaluation logic lives in `judges/` (single-shot judge client, the two mode judges, prompts); the FastAPI UI, evaluation orchestration, input parsing, video keyframe extraction, and history/export live in `web/`. Tests live in `tests/` and mirror the behavior they cover. Runtime configuration belongs in `config/judges.yaml` and `config/visual_modes/`; documentation is in `docs/`. Local datasets and generated run results belong in ignored `data/` and `runs/` directories.

## Build, Test, and Development Commands

Use Python 3.10 or later. Install the editable package and development/web dependencies with:

```bash
python -m pip install -e ".[dev,web]"
python -m pytest -q
python -m uvicorn auto_eval.web.server:app --host 127.0.0.1 --port 8503
```

The test command runs the suite with asyncio support enabled. The Uvicorn command serves both the API and static UI; there is no CLI entry point — all evaluation goes through the web UI or `auto_eval.web.runner`.

## Coding Style & Naming Conventions

Follow existing Python conventions: four-space indentation, `snake_case` for functions, variables, and modules, and `PascalCase` for classes and Pydantic models. Add type annotations to public functions and async boundaries. Prefer small, focused modules and reuse schemas/config models instead of passing untyped dictionaries. No formatter or linter is currently configured; match surrounding import order, docstrings, and formatting.

## Testing Guidelines

Write pytest tests as `tests/test_<feature>.py`, with test functions named `test_<behavior>`. Tests fake the judge client / eval call via `monkeypatch` so they do not call real LLMs or networks. Mark tests that need external services with `@pytest.mark.integration`; run focused checks, for example `python -m pytest -q tests/test_context.py`, before the full suite.

## Commit & Pull Request Guidelines

Recent history uses concise Conventional Commit-style subjects, such as `feat(web): ...` and `fix(judge): ...`; use `feat`, `fix`, or another clear type with an optional affected scope. Keep each commit narrowly scoped. PRs should explain the user-visible change, configuration/data implications, validation commands and results, and link related issues. Include screenshots for `web/static/` UI changes, and never commit `.env`, API keys, local datasets, or generated `runs/` output.
