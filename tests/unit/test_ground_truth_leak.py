"""Ground-truth leak test (spec 2.4 - "write an automated test that asserts this").

The seeded intent labels exist ONLY to score clustering. They must never appear
in any prompt payload, embedding input, or feature. Without this discipline the
purity metric measures nothing. This test scans every text that leaves the ticket
stages toward embeddings/prompts and fails if any seeded label string is found.
"""

from __future__ import annotations

import csv
from pathlib import Path

from kb_agent.tickets.adapters.sample import SampleAdapter
from kb_agent.tickets.clean import clean_tickets, embedding_texts
from kb_agent.tickets.loaders import load_tickets_bytes
from kb_agent.tickets.naming import name_clusters

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "sample_pack" / "tickets"


def _seeded_labels() -> set[str]:
    labels_file = PACK / "labels.csv"
    with labels_file.open(encoding="utf-8") as handle:
        return {row["seeded_intent"] for row in csv.DictReader(handle)}


def _load_sample_tickets():
    data = (PACK / "tickets.csv").read_bytes()
    rows = load_tickets_bytes(data, "csv")
    tickets, _ = SampleAdapter().adapt(rows)
    cleaned, _ = clean_tickets(tickets)
    return cleaned


def test_seeded_labels_absent_from_embedding_text() -> None:
    labels = _seeded_labels()
    assert labels  # sanity: we actually loaded ground truth
    tickets = _load_sample_tickets()
    blob = "\n".join(embedding_texts(tickets)).lower()
    leaked = [lab for lab in labels if lab.lower() in blob]
    assert not leaked, f"seeded labels leaked into embedding text: {leaked}"


def test_ticket_objects_do_not_carry_seeded_intent() -> None:
    tickets = _load_sample_tickets()
    for t in tickets[:50]:
        dump = t.model_dump()
        assert "seeded_intent" not in dump
        assert "_seeded_intent" not in dump


def test_naming_prompt_payload_has_no_seeded_labels() -> None:
    # Capture every prompt the naming stage sends and scan it.
    import numpy as np

    labels_set = _seeded_labels()
    tickets = _load_sample_tickets()[:40]

    captured: list[str] = []

    class CapturingLlm:
        model_id = "capture"

        def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str:
            captured.append(prompt)
            return '{"intent_name": "x", "description": "y", "coherence_flag": false}'

    # Two fake clusters over the first 40 tickets.
    cluster_labels = [0 if i % 2 == 0 else 1 for i in range(len(tickets))]
    reduced = np.random.RandomState(0).rand(len(tickets), 5)
    name_clusters(tickets, cluster_labels, reduced, CapturingLlm(), seed=42)

    joined = "\n".join(captured).lower()
    leaked = [lab for lab in labels_set if lab.lower() in joined]
    assert not leaked, f"seeded labels leaked into naming prompts: {leaked}"
    assert captured  # ensure we actually sent prompts
