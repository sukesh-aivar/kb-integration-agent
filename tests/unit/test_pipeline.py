"""End-to-end pipeline tests in fake mode (spec 3 resumability, 4.1, 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.pipeline import STAGE_ORDER, Pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "sample_pack" / "tickets"
CONFIG = load_config()


def _staged_clients(run_id: str = "t"):
    cs = build_fake_client_set()
    cs.store.put_bytes(
        f"uploads/{run_id}/tickets.csv", (PACK / "tickets.csv").read_bytes()
    )
    return cs


@pytest.mark.slow
def test_full_pipeline_fake_mode_produces_report_and_manifest() -> None:
    cs = _staged_clients("t")
    out = Pipeline(cs, CONFIG).run(
        AgentInput(run_id="t", ticket_uri="s3://b/uploads/t/tickets.csv"), golden=[]
    )
    assert out.status == "needs_review"
    assert out.stage == "write_report"

    # Report (MD + JSON) and manifest were written.
    assert cs.store.exists("runs/t/report.md")
    assert cs.store.exists("runs/t/report.json")
    assert cs.store.exists("runs/t/manifest.json")

    report = json.loads(cs.store.get_text("runs/t/report.json"))
    assert "SYNTHETIC" in report["synthetic_data_note"]
    assert report["status"] == "pending_review"
    assert report["metrics"]["ticket_count"] > 1400

    # Manifest records every stage and the config snapshot (spec 5.2).
    manifest = json.loads(cs.store.get_text("runs/t/manifest.json"))
    assert len(manifest["stages"]) == len(STAGE_ORDER)
    assert manifest["config_snapshot"]["seed"] == CONFIG.seed


@pytest.mark.slow
def test_drafts_only_under_drafts_prefix_never_kb_prefix() -> None:
    # Spec 2.2: unapproved drafts must not be reachable by the KB data source.
    cs = _staged_clients("t2")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="t2", ticket_uri="s3://b/uploads/t2/tickets.csv"), golden=[]
    )
    draft_keys = [k for k in cs.store.list_keys("drafts/")]
    kb_keys = [k for k in cs.store.list_keys("runs/t2/kb-docs")]
    assert draft_keys  # articles were drafted
    # No draft article leaked into the KB data-source prefix.
    assert not any(k.startswith("runs/t2/kb-docs") and "article" in k.lower() for k in kb_keys)


def test_resume_from_unknown_stage_raises() -> None:
    cs = _staged_clients("t3")
    with pytest.raises(ValueError):
        Pipeline(cs, CONFIG).run(
            AgentInput(
                run_id="t3",
                ticket_uri="s3://b/uploads/t3/tickets.csv",
                resume_from="not_a_stage",
            ),
            golden=[],
        )


def test_pipeline_handles_no_tickets_gracefully() -> None:
    # Spec 6: skip inapplicable stages. No tickets, no docs -> still completes.
    cs = build_fake_client_set()
    out = Pipeline(cs, CONFIG).run(AgentInput(run_id="empty"), golden=[])
    assert out.status == "needs_review"
    assert cs.store.exists("runs/empty/report.json")
