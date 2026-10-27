"""DORMANT: the full ticket+join pipeline tests.

Moved out of tests/unit/test_pipeline.py in the v3 KB-only reorg. The active
Pipeline no longer dispatches the ticket/join stages, so to exercise the full
13-stage workflow these tests compose a Pipeline subclass that inherits
DormantStagesMixin and runs LEGACY_STAGE_ORDER_13. This is the documented
re-enable path for the dormant stages.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dormant

import json
from pathlib import Path

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.dormant.pipeline_stages import DormantStagesMixin
from kb_agent.pipeline import LEGACY_STAGE_ORDER_13, Pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "dormant" / "sample_pack" / "tickets"
CONFIG = load_config()


class FullPipeline(DormantStagesMixin, Pipeline):
    """Pipeline with the dormant ticket/join stages re-enabled (dormant tests only)."""


def _run_full(cs, agent_input, golden=None):
    # Monkeypatch the module-level STAGE_ORDER the driver reads, for the full run.
    import kb_agent.pipeline as pl

    original = pl.STAGE_ORDER
    pl.STAGE_ORDER = LEGACY_STAGE_ORDER_13
    try:
        return FullPipeline(cs, CONFIG).run(agent_input, golden=golden or [])
    finally:
        pl.STAGE_ORDER = original


def _staged_clients(run_id: str = "t"):
    cs = build_fake_client_set()
    cs.store.put_bytes(f"uploads/{run_id}/tickets.csv", (PACK / "tickets.csv").read_bytes())
    return cs


@pytest.mark.slow
def test_full_pipeline_fake_mode_produces_report_and_manifest() -> None:
    cs = _staged_clients("t")
    out = _run_full(
        cs, AgentInput(run_id="t", ticket_uri="s3://b/uploads/t/tickets.csv"), golden=[]
    )
    # Part B: terminal status is 'completed' (no human-review gate; the legacy
    # needs_review re-emit is opt-in via workflow.legacy_status_needs_review).
    assert out.status == "completed"
    assert out.stage == "write_report"

    assert cs.store.exists("runs/t/report.md")
    assert cs.store.exists("runs/t/manifest.json")

    manifest = json.loads(cs.store.get_text("runs/t/manifest.json"))
    assert len(manifest["stages"]) == len(LEGACY_STAGE_ORDER_13)


@pytest.mark.slow
def test_drafts_only_under_drafts_prefix_never_kb_prefix() -> None:
    cs = _staged_clients("t2")
    _run_full(cs, AgentInput(run_id="t2", ticket_uri="s3://b/uploads/t2/tickets.csv"), golden=[])
    draft_keys = list(cs.store.list_keys("drafts/"))
    kb_keys = list(cs.store.list_keys("runs/t2/kb-docs"))
    assert not any(
        k.startswith("runs/t2/kb-docs") and "article" in k.lower() for k in kb_keys
    )
    # draft_keys may be empty if no gaps were drafted; the invariant is that no
    # draft leaked into the KB prefix (checked above).
    assert isinstance(draft_keys, list)
