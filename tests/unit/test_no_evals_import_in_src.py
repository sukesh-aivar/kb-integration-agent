"""Guard: src/ must not import the evals package (spec 7.4 layering).

The runtime pipeline runs with only ``src`` on the path (e.g. in the container and
via scripts/run_local.py). ``evals`` depends on ``src``, never the reverse - a
runtime ``import evals`` crashes the pipeline with ModuleNotFoundError. This test
fails fast if any src module imports evals, catching the regression that twice
broke ``make run-fake``.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def test_src_does_not_import_evals() -> None:
    offenders: list[str] = []
    pattern = re.compile(r"^\s*(from\s+evals|import\s+evals)\b", re.MULTILINE)
    for py in SRC.rglob("*.py"):
        if pattern.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(SRC)))
    assert not offenders, f"src must not import evals (runtime dependency): {offenders}"
