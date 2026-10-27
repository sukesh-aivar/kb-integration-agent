"""Layering guards (spec 7.4): src must not import evals; active src must not
import kb_agent.dormant (v3 KB-only reorg, A6).

* ``evals`` depends on ``src``, never the reverse — a runtime ``import evals``
  crashes the pipeline with ModuleNotFoundError.
* The active package (``src/kb_agent/`` excluding ``dormant/``) must never import
  ``kb_agent.dormant``: dormant code is excluded from the deployed image, so an
  active import of it would crash the container. Checked via AST so a commented
  ``# DORMANT: ...`` import line is (correctly) invisible.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
ACTIVE_PKG = SRC / "kb_agent"
DORMANT_PKG = ACTIVE_PKG / "dormant"


def test_src_does_not_import_evals() -> None:
    offenders: list[str] = []
    pattern = re.compile(r"^\s*(from\s+evals|import\s+evals)\b", re.MULTILINE)
    for py in SRC.rglob("*.py"):
        if pattern.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(SRC)))
    assert not offenders, f"src must not import evals (runtime dependency): {offenders}"


def _imports_dormant(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "kb_agent.dormant" or a.name.startswith("kb_agent.dormant.")
                   for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "kb_agent.dormant" or node.module.startswith("kb_agent.dormant."):
                return True
    return False


def test_active_src_does_not_import_dormant() -> None:
    offenders: list[str] = []
    for py in ACTIVE_PKG.rglob("*.py"):
        # Skip the dormant package itself — it may reference its own submodules.
        if DORMANT_PKG in py.parents:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        if _imports_dormant(tree):
            offenders.append(str(py.relative_to(SRC)))
    assert not offenders, (
        f"active src must NOT import kb_agent.dormant (excluded from the image): {offenders}"
    )
