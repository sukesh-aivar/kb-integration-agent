"""Unit tests for the blue-pill stages (spec 4.3, 7.5)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dormant


import json
from pathlib import Path

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.dormant.tickets.adapters.sample import SampleAdapter
from kb_agent.dormant.tickets.categorise import categorise_tickets
from kb_agent.dormant.tickets.clean import clean_text, clean_tickets, embedding_texts
from kb_agent.dormant.tickets.embed import EmbeddingCache
from kb_agent.dormant.tickets.loaders import load_tickets_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "dormant" / "sample_pack" / "tickets"
CONFIG = load_config()


# --- loaders -----------------------------------------------------------------


def test_load_csv_and_json_equivalent_shape() -> None:
    csv_bytes = b"ticket_id,subject,description\nT1,Hi,there\n"
    rows = load_tickets_bytes(csv_bytes, "csv")
    assert rows == [{"ticket_id": "T1", "subject": "Hi", "description": "there"}]

    json_bytes = json.dumps(
        {"tickets": [{"ticket_id": "T1", "subject": "Hi", "description": "there"}]}
    ).encode()
    assert load_tickets_bytes(json_bytes, "json") == rows


# --- adapter -----------------------------------------------------------------


def test_sample_adapter_maps_and_reports() -> None:
    rows = [
        {
            "ticket_id": "T1",
            "subject": "Cannot log in",
            "description": "forgot password",
            "resolution": "reset it",
            "status": "solved",
            "created_at": "2026-01-01T00:00:00+00:00",
            "resolved_at": "2026-01-01T01:00:00+00:00",
            "tags": "password",
        },
        {"ticket_id": "", "subject": "", "description": ""},  # unmappable -> skipped
    ]
    tickets, report = SampleAdapter().adapt(rows)
    assert report.mapped == 1
    assert report.skipped == 1
    assert tickets[0].ticket_id == "T1"
    assert tickets[0].status == "solved"
    assert tickets[0].tags == ["password"]


def test_adapter_maps_alternative_column_names() -> None:
    # A zendesk-ish export using short_description / body / state.
    rows = [
        {
            "id": "Z9",
            "short_description": "App crash",
            "body": "crashes on open",
            "state": "new",
            "created": "2026-01-02T00:00:00+00:00",
        }
    ]
    tickets, report = SampleAdapter().adapt(rows)
    assert report.mapped == 1
    assert tickets[0].ticket_id == "Z9"
    assert tickets[0].subject == "App crash"
    assert tickets[0].status == "open"  # 'new' -> open


def test_adapter_ignores_forbidden_label_column() -> None:
    # Even if a seeded label is present, it must not reach the ticket.
    rows = [
        {
            "ticket_id": "T1",
            "subject": "x",
            "description": "y",
            "created_at": "2026-01-01T00:00:00+00:00",
            "seeded_intent": "password_reset",
        }
    ]
    tickets, _ = SampleAdapter().adapt(rows)
    assert "password_reset" not in tickets[0].model_dump_json()


# --- clean -------------------------------------------------------------------


def test_clean_strips_signature_and_quotes() -> None:
    body = "The sync fails.\n> previous message\nRegards,\nAlex"
    cleaned = clean_text(body)
    assert "previous message" not in cleaned
    assert "Regards" not in cleaned
    assert "sync fails" in cleaned


def test_clean_dedupes_identical_tickets() -> None:
    ts = "2026-01-01T00:00:00+00:00"
    rows = [
        {"ticket_id": "T1", "subject": "same", "description": "body", "created_at": ts},
        {"ticket_id": "T2", "subject": "same", "description": "body", "created_at": ts},
    ]
    tickets, _ = SampleAdapter().adapt(rows)
    unique, dupes = clean_tickets(tickets)
    assert len(unique) == 1
    assert dupes == 1


def test_embedding_text_excludes_resolution() -> None:
    rows = [
        {
            "ticket_id": "T1",
            "subject": "Login problem",
            "description": "cannot sign in",
            "resolution": "SECRETFIXWORD applied",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    tickets, _ = SampleAdapter().adapt(rows)
    texts = embedding_texts(tickets)
    # Resolution text must NOT be embedded (spec 4.3).
    assert "SECRETFIXWORD" not in texts[0]
    assert "cannot sign in" in texts[0]


# --- embed cache -------------------------------------------------------------


def test_embedding_cache_avoids_recompute() -> None:
    cs = build_fake_client_set()

    class CountingEmbeddings:
        model_id = "count-v1"
        calls = 0
        total = 0

        def embed(self, texts):
            CountingEmbeddings.calls += 1
            CountingEmbeddings.total += len(texts)
            return cs.embeddings.embed(texts)

    client = CountingEmbeddings()
    cache = EmbeddingCache()
    texts = ["alpha", "beta", "alpha"]
    out1 = cache.get_or_compute(texts, client)
    # Only 2 unique texts computed despite 3 inputs.
    assert CountingEmbeddings.total == 2
    assert len(out1) == 3
    # Second call: all cached, no new compute.
    cache.get_or_compute(texts, client)
    assert CountingEmbeddings.total == 2


# --- categorise (<500 path) --------------------------------------------------


def test_categorise_uses_candidates_not_seeded_labels() -> None:
    cs = build_fake_client_set()
    rows = [
        {
            "ticket_id": "T1",
            "subject": "login",
            "description": "cannot sign in",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    tickets, _ = SampleAdapter().adapt(rows)
    candidates = ["sign_in_issue", "billing_issue"]  # NOT the seeded labels
    labels, version = categorise_tickets(tickets, candidates, cs.llm)
    assert len(labels) == 1
    assert labels[0] in set(candidates) | {"uncategorised"}
    assert version == "categorise/v1"
