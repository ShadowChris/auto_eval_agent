"""Project-local path helpers.

Keep user-facing paths in .env/config relative, but resolve them against the
auto_eval_agent project root instead of the process current working directory.
This prevents service launches from different directories from creating
multiple runs/ trees.
"""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = PROJECT_ROOT / "runs"


def resolve_project_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p
