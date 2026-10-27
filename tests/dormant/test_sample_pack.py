"""Unit tests for the sample pack generator (spec 2.3 reproducibility, 2.4 leak).

Offline. These assert the two properties that make the pack trustworthy:
reproducibility, and that ground-truth labels never appear in the customer-facing
ticket data.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dormant

from datetime import UTC, datetime

from dormant.scripts.generate_tickets import INTENTS, TICKET_COLUMNS, generate


@pytest.fixture(scope="module")
def tickets():
    # Fixed seed + fixed reference time -> deterministic.
    return generate(per_intent=10, seed=42, now=datetime(2026, 1, 1, tzinfo=UTC))


def test_exactly_twenty_intents() -> None:
    assert len(INTENTS) == 20
    assert len({i.label for i in INTENTS}) == 20  # all distinct


def test_generation_is_reproducible() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = generate(per_intent=10, seed=42, now=now)
    b = generate(per_intent=10, seed=42, now=now)
    assert [t.ticket_id for t in a] == [t.ticket_id for t in b]
    assert [t.description for t in a] == [t.description for t in b]


def test_ticket_columns_exclude_ground_truth() -> None:
    # The customer-facing schema must not carry the label or edge-case flag.
    assert "seeded_intent" not in TICKET_COLUMNS
    assert "edge_case" not in TICKET_COLUMNS


def test_all_edge_cases_are_seeded(tickets) -> None:
    seen = {t.edge_case for t in tickets}
    for required in ("pii", "non_english", "duplicate", "no_resolution"):
        assert required in seen, f"missing seeded edge case: {required}"


def test_no_resolution_rows_have_no_resolution(tickets) -> None:
    for t in tickets:
        if t.edge_case == "no_resolution":
            assert t.resolution is None
            assert t.resolved_at is None


def test_labels_cover_every_ticket(tickets) -> None:
    # Every ticket must have a ground-truth label available for scoring.
    for t in tickets:
        assert t.seeded_intent
        assert t.seeded_intent in {i.label for i in INTENTS}


def test_time_split_is_roughly_seventy_thirty(tickets) -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    from datetime import timedelta

    boundary = cutoff - timedelta(days=100)
    older = sum(1 for t in tickets if t.created_at < boundary)
    share = older / len(tickets)
    # Allow slack: it is a random split around 0.70.
    assert 0.6 <= share <= 0.8
