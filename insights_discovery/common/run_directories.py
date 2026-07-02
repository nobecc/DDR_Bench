#!/usr/bin/env python3
"""Helpers for timestamped insight-discovery experiment directories."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


RUN_PREFIX = "runs_"


def new_run_name() -> str:
    return f"{RUN_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def is_run_dir(path: Path) -> bool:
    return path.name.startswith(RUN_PREFIX)


def ensure_run_dir(output_root: Path, run_name: str | None = None) -> Path:
    """Return a run directory, avoiding duplicate runs_* nesting."""

    if is_run_dir(output_root):
        run_dir = output_root
    else:
        run_dir = output_root / (run_name or new_run_name())
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def path_contains_run_dir(path: Path) -> bool:
    return any(part.startswith(RUN_PREFIX) for part in path.parts)


def latest_run_dir(output_root: Path) -> Path:
    """Resolve a method root to its newest runs_* child."""

    if is_run_dir(output_root):
        return output_root
    candidates = [path for path in output_root.glob(f"{RUN_PREFIX}*") if path.is_dir()]
    if not candidates:
        return output_root
    return max(candidates, key=lambda path: path.stat().st_mtime)
