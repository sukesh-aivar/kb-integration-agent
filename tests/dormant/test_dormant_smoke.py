"""DORMANT smoke test: every dormant module still imports.

Leaving dormant code importable (not deleted) is the point of the reorg — this
catches a dormant module that rots after a move or an import-path change.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.dormant

DORMANT_MODULES = [
    "kb_agent.dormant.prompts",
    "kb_agent.dormant.llm_json",
    "kb_agent.dormant.documents_extras",
    "kb_agent.dormant.pipeline_stages",
    "kb_agent.dormant.tickets.loaders",
    "kb_agent.dormant.tickets.adapters.base",
    "kb_agent.dormant.tickets.adapters.sample",
    "kb_agent.dormant.tickets.clean",
    "kb_agent.dormant.tickets.embed",
    "kb_agent.dormant.tickets.cluster",
    "kb_agent.dormant.tickets.categorise",
    "kb_agent.dormant.tickets.naming",
    "kb_agent.dormant.join.coverage",
    "kb_agent.dormant.join.drafting",
    "kb_agent.dormant.join.report_sections",
    "kb_agent.dormant.review.state",
    "kb_agent.dormant.planner.planner",
    "kb_agent.dormant.planner.tools",
    "dormant.evals.metrics",
]


@pytest.mark.parametrize("module", DORMANT_MODULES)
def test_dormant_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_dormant_prompts_dir_points_at_dormant_config() -> None:
    from kb_agent.dormant.prompts import PROMPTS_DIR

    assert PROMPTS_DIR.parts[-2:] == ("dormant", "prompts")
    # all five dormant templates are present
    for name in ("naming", "categorise", "coverage", "drafting", "planner"):
        assert (PROMPTS_DIR / f"{name}.v1.md").exists()
