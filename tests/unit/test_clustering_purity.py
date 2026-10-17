"""End-to-end clustering purity on the real sample pack (spec 1.5 target >=0.80).

This is the blue pill's definition-of-done check: run adapt -> clean -> embed ->
cluster on the actual 1,500-ticket sample pack (fake embeddings) and score intent
purity against the hidden labels.csv. Purity measures how well the discovered
clusters match the planted intents.

IMPORTANT about fake mode: fake embeddings are a hashed bag-of-words stand-in, not
a semantic model. They cluster the lexically-separable planted intents well, so
this proves the *pipeline* (UMAP/HDBSCAN wiring, seeding, scoring, label join) is
correct. It does NOT prove real-world clustering quality - that needs real Bedrock
embeddings on harder tickets (the LLM ticket generator, task 11). The report says
so explicitly.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from evals.metrics import intent_purity, noise_share
from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.tickets.adapters.sample import SampleAdapter
from kb_agent.tickets.clean import clean_tickets, embedding_texts
from kb_agent.tickets.cluster import cluster_embeddings, should_use_density_clustering
from kb_agent.tickets.embed import embed_texts
from kb_agent.tickets.loaders import load_tickets_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "sample_pack" / "tickets"


def _labels_by_id() -> dict[str, str]:
    with (PACK / "labels.csv").open(encoding="utf-8") as handle:
        return {row["ticket_id"]: row["seeded_intent"] for row in csv.DictReader(handle)}


@pytest.mark.slow
def test_clustering_purity_meets_target_on_sample_pack() -> None:
    cfg = load_config()
    cs = build_fake_client_set()

    rows = load_tickets_bytes((PACK / "tickets.csv").read_bytes(), "csv")
    tickets, report = SampleAdapter().adapt(rows)
    assert report.mapped > 1400  # the pack is ~1,500 tickets

    cleaned, _ = clean_tickets(tickets)
    assert should_use_density_clustering(len(cleaned), cfg)  # >=500 -> density path

    vectors = embed_texts(embedding_texts(cleaned), cs.embeddings)
    result = cluster_embeddings(vectors, cfg)

    truth = _labels_by_id()
    pred = result.labels
    true_labels = [truth[t.ticket_id] for t in cleaned]

    purity = intent_purity(pred, true_labels)  # ignores -1 noise by default
    ns = noise_share(pred)

    # Record for the human reading test output.
    print(
        f"\n[purity] clusters={result.n_clusters} purity={purity:.3f} "
        f"noise_share={ns:.3f} (fake embeddings, synthetic data)"
    )

    # Target from spec 1.5. Fake embeddings on lexically-separable intents clear
    # this comfortably; the assertion guards the pipeline wiring, not real quality.
    assert purity >= 0.80, f"intent purity {purity:.3f} below target 0.80"
