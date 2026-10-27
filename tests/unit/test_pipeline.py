"""End-to-end pipeline tests — KB-only workflow (v3), fake mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.pipeline import STAGE_ORDER, Pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "sample_pack" / "documents"
CONFIG = load_config()


def _staged_docs_clients(run_id: str, names: list[str]):
    cs = build_fake_client_set()
    uris = []
    for n in names:
        cs.store.put_bytes(f"uploads/{run_id}/docs/{n}", (DOCS / n).read_bytes())
        uris.append(f"s3://b/uploads/{run_id}/docs/{n}")
    return cs, uris


@pytest.mark.slow
def test_kb_only_run_produces_report_and_manifest() -> None:
    cs, uris = _staged_docs_clients("t", ["faq.md", "installation.md"])
    out = Pipeline(cs, CONFIG).run(AgentInput(run_id="t", document_uris=uris), golden=[])
    assert out.status == "completed"
    assert out.stage == "write_report"
    assert out.pending_review == []  # no approval step in the KB-only workflow

    assert cs.store.exists("runs/t/report.md")
    assert cs.store.exists("runs/t/report.json")
    assert cs.store.exists("runs/t/manifest.json")

    report = json.loads(cs.store.get_text("runs/t/report.json"))
    assert report["workflow"] == "kb_only"
    assert report["status"] == "pending_review"
    # Dormant sections are present-but-empty (rule 5: keys kept).
    assert report["intent_view"] == []
    assert report["coverage"] == []
    assert report["draft_articles"] == []
    assert report["drafting_summary"] == {}
    assert report["metrics"]["document_count"] == 2

    # Exactly the four KB-only stages are recorded (none skipped in kb_only mode).
    manifest = json.loads(cs.store.get_text("runs/t/manifest.json"))
    assert len(manifest["stages"]) == len(STAGE_ORDER) == 4
    assert [s["stage"] for s in manifest["stages"]] == STAGE_ORDER


def test_resume_from_unknown_stage_raises() -> None:
    cs, uris = _staged_docs_clients("t3", ["faq.md"])
    with pytest.raises(ValueError):
        Pipeline(cs, CONFIG).run(
            AgentInput(run_id="t3", document_uris=uris, resume_from="not_a_stage"),
            golden=[],
        )


def test_resume_from_dormant_stage_is_rejected() -> None:
    # A4.1: resume_from naming a now-dormant stage is invalid input.
    cs, uris = _staged_docs_clients("t4", ["faq.md"])
    with pytest.raises(ValueError):
        Pipeline(cs, CONFIG).run(
            AgentInput(run_id="t4", document_uris=uris, resume_from="coverage_check"),
            golden=[],
        )


def test_pipeline_handles_no_documents_gracefully() -> None:
    # No documents, no tickets -> still completes (stages run but no-op).
    cs = build_fake_client_set()
    out = Pipeline(cs, CONFIG).run(AgentInput(run_id="empty"), golden=[])
    assert out.status == "completed"
    assert cs.store.exists("runs/empty/report.json")


def test_ticket_uri_is_accepted_and_ignored() -> None:
    # A4.5: a ticket export is accepted and ignored in the KB-only workflow.
    cs, uris = _staged_docs_clients("tk", ["faq.md"])
    out = Pipeline(cs, CONFIG).run(
        AgentInput(run_id="tk", document_uris=uris, ticket_uri="s3://b/uploads/tk/tickets.csv"),
        golden=[],
        emit_runstate=True,
    )
    assert out.status == "completed"
    # The ignored-ticket info event is emitted exactly once.
    events = []
    for k in sorted(cs.store.list_keys("runs/tk/_events/")):
        events.append(json.loads(cs.store.get_text(k)))
    ignored = [e for e in events if "ticket export was supplied and ignored" in e.get("text", "")]
    assert len(ignored) == 1
