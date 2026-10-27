"""Unit tests for the join stages: coverage, drafting, report, review (spec 4.4)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dormant

import json
from datetime import UTC, datetime

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import RunMetrics
from kb_agent.dormant.join.coverage import coverage_check, false_positive_rate_from_items
from kb_agent.dormant.join.drafting import DRAFTS_PREFIX, draft_articles
from kb_agent.dormant.review.state import ReviewRegistry
from kb_agent.join.report import SYNTHETIC_NOTE, build_report, render_markdown
from kb_agent.models import CoverageItem, CoverageStatus, IntentCluster, ReviewStatus, Ticket

CONFIG = load_config()


def _ticket(tid: str, resolved: bool = True) -> Ticket:
    return Ticket(
        ticket_id=tid,
        subject="Cannot reset password",
        description="the reset email never arrives when I try to reset my password",
        resolution="Triggered a manual password reset." if resolved else None,
        status="solved" if resolved else "open",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- drafting: the safety-critical rules -------------------------------------


def _cluster(cid: int, name: str, example_ids: list[str]) -> IntentCluster:
    return IntentCluster(
        cluster_id=cid,
        intent_name=name,
        description=f"about {name}",
        volume=len(example_ids),
        resolved_share=1.0,
        example_ticket_ids=example_ids,
    )


def _gap(name: str) -> list[CoverageItem]:
    """A coverage list marking ``name`` as a GAP (so it is eligible to draft)."""
    return [CoverageItem(intent_name=name, status=CoverageStatus.GAP, volume=6)]


def test_drafting_requires_min_resolved_tickets() -> None:
    cs = build_fake_client_set()
    tickets = [_ticket(f"T{i:05d}") for i in range(3)]  # only 3 resolved (<5)
    labels = [0, 0, 0]
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    outcomes, _ = draft_articles(
        clusters, labels, tickets, _gap("password_reset"), run_id="r1",
        llm=cs.llm, guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is False
    assert "resolved tickets" in outcomes[0].reason


def test_drafting_only_runs_for_gaps_not_covered_intents() -> None:
    # Spec 4.4: an intent the KB already covers must NOT be drafted.
    cs = build_fake_client_set()
    tickets = [_ticket(f"T{i:05d}") for i in range(6)]  # >=5 resolved
    labels = [0] * 6
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    covered = [CoverageItem(intent_name="password_reset", status=CoverageStatus.COVERED, volume=6)]
    outcomes, _ = draft_articles(
        clusters, labels, tickets, covered, run_id="r1",
        llm=cs.llm, guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is False
    assert "covered" in outcomes[0].reason
    # Nothing written.
    assert not [k for k in cs.store.list_keys("") if k.endswith(".json")]


def test_drafting_skips_untested_intents() -> None:
    cs = build_fake_client_set()
    tickets = [_ticket(f"T{i:05d}") for i in range(6)]
    labels = [0] * 6
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    # Empty coverage list -> intent was never tested -> not drafted.
    outcomes, _ = draft_articles(
        clusters, labels, tickets, [], run_id="r1",
        llm=cs.llm, guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is False
    assert "not among coverage-tested" in outcomes[0].reason


def test_drafting_writes_only_to_drafts_prefix() -> None:
    cs = build_fake_client_set()
    tickets = [_ticket(f"T{i:05d}") for i in range(6)]  # >=5 resolved
    labels = [0] * 6
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    outcomes, _ = draft_articles(
        clusters, labels, tickets, _gap("password_reset"), run_id="r1",
        llm=cs.llm, guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is True
    keys = cs.store.list_keys("")
    # The only written article key is under drafts/r1/, never a KB prefix.
    article_keys = [k for k in keys if k.endswith(".json")]
    assert article_keys
    for k in article_keys:
        assert k.startswith(f"{DRAFTS_PREFIX}/r1/")


def test_drafting_rejects_bad_citations() -> None:
    cs = build_fake_client_set()

    # An LLM that cites a ticket id that is NOT in the intent.
    class BadCiteLlm:
        model_id = "bad-cite"

        def invoke(self, prompt, *, max_tokens=1024, temperature=0.0):
            return json.dumps(
                {
                    "title": "x", "problem": "p", "applies_to": "a",
                    "steps": ["s"], "resolution": "r",
                    "source_ticket_ids": ["T99999"],  # not in the intent
                }
            )

    tickets = [_ticket(f"T{i:05d}") for i in range(6)]
    labels = [0] * 6
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    outcomes, _ = draft_articles(
        clusters, labels, tickets, _gap("password_reset"), run_id="r1",
        llm=BadCiteLlm(), guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is False
    assert "citations not in intent" in outcomes[0].reason


def test_drafting_blocks_pii_in_generated_text() -> None:
    cs = build_fake_client_set()

    # An LLM that leaks an email into the drafted article body.
    class PiiLlm:
        model_id = "pii-llm"

        def invoke(self, prompt, *, max_tokens=1024, temperature=0.0):
            ids = [f"T{i:05d}" for i in range(6)]
            return json.dumps(
                {
                    "title": "Reset", "problem": "contact admin@example.com",
                    "applies_to": "all", "steps": ["s"], "resolution": "r",
                    "source_ticket_ids": ids,
                }
            )

    tickets = [_ticket(f"T{i:05d}") for i in range(6)]
    labels = [0] * 6
    clusters = [_cluster(0, "password_reset", [t.ticket_id for t in tickets])]
    outcomes, _ = draft_articles(
        clusters, labels, tickets, _gap("password_reset"), run_id="r1",
        llm=PiiLlm(), guardrail=cs.guardrail, store=cs.store, config=CONFIG,
    )
    assert outcomes[0].drafted is False
    assert "PII" in outcomes[0].reason
    # Blocked draft must NOT be written as an approvable file.
    assert not [k for k in cs.store.list_keys("") if k.endswith(".json")]


# --- coverage ----------------------------------------------------------------


def test_coverage_flags_gap_when_kb_empty() -> None:
    from kb_agent.documents.knowledge_base import KnowledgeBaseStage

    cs = build_fake_client_set()
    stage = KnowledgeBaseStage(cs.knowledge_base, CONFIG)
    stage.create("kb", "kb/empty")  # nothing ingested
    stage.ingest()

    tickets = {f"T{i:05d}": _ticket(f"T{i:05d}") for i in range(3)}
    clusters = [_cluster(0, "password_reset", list(tickets.keys()))]
    items, version = coverage_check(clusters, tickets, stage, cs.llm, CONFIG)
    assert items[0].status == CoverageStatus.GAP
    assert version == "coverage/v1"


def test_coverage_fp_rate_penalises_flag_everything() -> None:
    items = [
        CoverageItem(intent_name="a", status=CoverageStatus.GAP, volume=10),
        CoverageItem(intent_name="b", status=CoverageStatus.GAP, volume=5),
    ]
    # Both are actually covered -> flagging both is 100% false positives.
    fp = false_positive_rate_from_items(items, {"a": True, "b": True})
    assert fp == 1.0


# --- review state ------------------------------------------------------------


def test_review_registry_transitions_and_rejects_illegal() -> None:
    import pytest

    reg = ReviewRegistry()
    reg.register("art1", "article", model_id="m1", prompt_version="drafting/v1")
    assert reg.pending()

    reg.transition("art1", ReviewStatus.APPROVED, reviewer="alex", comment="ok")
    state = reg.get("art1")
    assert state.status == ReviewStatus.APPROVED
    assert state.history[-1].reviewer == "alex"
    assert state.history[-1].model_id == "m1"

    # Approving an already-approved artefact is illegal.
    from kb_agent.exceptions import KBAgentError

    with pytest.raises(KBAgentError):
        reg.transition("art1", ReviewStatus.APPROVED, reviewer="alex")


# --- report ------------------------------------------------------------------


def test_report_includes_synthetic_note_md_and_json() -> None:
    metrics = RunMetrics(recall_at_5=0.8, intent_purity=0.95, cluster_count=20)
    clusters = [_cluster(0, "password_reset", ["T00000"])]
    coverage = [CoverageItem(intent_name="password_reset", status=CoverageStatus.COVERED, volume=5)]
    report = build_report(
        run_id="r1", kb_id="kb-1", metrics=metrics,
        clusters=clusters, coverage=coverage, drafts=[],
    )
    assert report["synthetic_data_note"] == SYNTHETIC_NOTE
    assert report["status"] == "pending_review"
    md = render_markdown(report)
    assert "SYNTHETIC" in md
    assert "recall@5" in md
