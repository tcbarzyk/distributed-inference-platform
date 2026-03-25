"""Comparison helpers for benchmark runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .profiles import compare_runs


def load_run(path: str | Path) -> dict[str, Any]:
    """Load a benchmark run JSON document."""
    run_path = Path(path)
    return json.loads(run_path.read_text(encoding="utf-8"))


def compare_run_files(old_path: str | Path, new_path: str | Path) -> dict[str, Any]:
    """Compare two benchmark files."""
    return compare_runs(load_run(old_path), load_run(new_path))
