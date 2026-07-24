# Repository Guidelines

## Project Structure & Module Organization

Application code is in `src/auto_eval/`. Keep domain models in `schema.py`, configuration and path helpers in `config.py` and `paths.py`, and place feature logic in its existing area: `runners/` for model adapters, `judges/` for evaluation, `batch/` for orchestration, `analysis/` and `report/` for outputs, and `web/` for the FastAPI UI. Tests live in `tests/` and mirror the behavior they cover. Runtime configuration belongs in `config/*.yaml` (including `config/skills/`); documentation and diagrams are in `docs/` and `assets/`. Local datasets and generated run results belong in ignored `data/` and `runs/` directories.

## Build, Test, and Development Commands

Use Python 3.10 or later. Install the editable package and development/web dependencies with:

```bash
python -m pip install -e ".[dev,web]"
python -m pytest -q
python -m uvicorn auto_eval.web.server:app --host 127.0.0.1 --port 8502
```

The test command runs the suite with asyncio support enabled. The Uvicorn command serves both the API and static UI. For a CLI smoke run, after configuring `.env` and YAML files, use `python -m auto_eval.cli run -d data/dataset.jsonl --limit 20`.

## Coding Style & Naming Conventions

Follow existing Python conventions: four-space indentation, `snake_case` for functions, variables, and modules, and `PascalCase` for classes and Pydantic models. Add type annotations to public functions and async boundaries. Prefer small, focused modules and reuse schemas/config models instead of passing untyped dictionaries. No formatter or linter is currently configured; match surrounding import order, docstrings, and formatting.

## Testing Guidelines

Write pytest tests as `tests/test_<feature>.py`, with test functions named `test_<behavior>`. Reuse fakes and fixtures from `tests/conftest.py` so ordinary tests do not call real LLMs or networks. Mark tests that need external services with `@pytest.mark.integration`; run focused checks, for example `python -m pytest -q tests/test_context.py`, before the full suite.

## Commit & Pull Request Guidelines

Recent history uses concise Conventional Commit-style subjects, such as `feat(web): ...` and `fix(judge): ...`; use `feat`, `fix`, or another clear type with an optional affected scope. Keep each commit narrowly scoped. PRs should explain the user-visible change, configuration/data implications, validation commands and results, and link related issues. Include screenshots for `web/static/` UI changes, and never commit `.env`, API keys, local datasets, or generated `runs/` output.
