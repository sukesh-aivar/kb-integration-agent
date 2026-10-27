"""DORMANT eval metrics — clustering/retrieval/coverage scoring.

Moved verbatim from ``evals/metrics.py`` in the v3 KB-only reorg. These score the
dormant ticket/coverage workflow (intent purity, silhouette, cluster count, noise
share, recall@k, coverage false-positive rate). The active eval harness keeps
only ``routing_accuracy`` in ``evals/metrics.py``.

Every function is pure (no AWS, no network, no file I/O).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any


def intent_purity(
    predicted_labels: Sequence[int | str],
    true_labels: Sequence[int | str],
    ignore_label: int | str | None = -1,
) -> float:
    """Cluster purity against ground-truth intents.

    Purity assigns each predicted cluster the ground-truth label that is most
    common within it, then counts how many points were thereby labelled
    correctly, divided by the total number of scored points.

    ``purity = (1/N) * sum over clusters of (count of the majority true label)``
    """
    if len(predicted_labels) != len(true_labels):
        raise ValueError(
            f"label length mismatch: {len(predicted_labels)} predicted "
            f"vs {len(true_labels)} true"
        )

    clusters: dict[int | str, list[int | str]] = defaultdict(list)
    for pred, true in zip(predicted_labels, true_labels, strict=False):
        if ignore_label is not None and pred == ignore_label:
            continue
        clusters[pred].append(true)

    scored = sum(len(members) for members in clusters.values())
    if scored == 0:
        return 0.0

    correct = 0
    for members in clusters.values():
        correct += Counter(members).most_common(1)[0][1]
    return correct / scored


def noise_share(predicted_labels: Sequence[int | str], noise_label: int | str = -1) -> float:
    """Fraction of items left in the noise bucket (spec 1.5 diagnostic)."""
    if not predicted_labels:
        return 0.0
    noise = sum(1 for p in predicted_labels if p == noise_label)
    return noise / len(predicted_labels)


def cluster_count(predicted_labels: Sequence[int | str], noise_label: int | str = -1) -> int:
    """Number of real clusters discovered, excluding the noise bucket."""
    return len({p for p in predicted_labels if p != noise_label})


def silhouette(
    embeddings: Any,
    predicted_labels: Sequence[int],
    metric: str = "cosine",
) -> float:
    """Silhouette score of a clustering (spec 1.5 over-split/merge diagnostic)."""
    from sklearn.metrics import silhouette_score  # local import keeps module pure/light

    labels = list(predicted_labels)
    distinct = {label for label in labels}
    if len(distinct) < 2 or len(distinct) >= len(labels):
        return 0.0
    return float(silhouette_score(embeddings, labels, metric=metric))


def recall_at_k(
    retrieved: Sequence[Sequence[str]],
    expected: Sequence[str],
    k: int = 5,
) -> float:
    """Recall@k over a golden retrieval set (spec 1.5, target >= 0.80)."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if len(retrieved) != len(expected):
        raise ValueError(
            f"length mismatch: {len(retrieved)} retrieved vs {len(expected)} expected"
        )
    if not expected:
        return 0.0

    hits = 0
    for got, want in zip(retrieved, expected, strict=False):
        if want in list(got)[:k]:
            hits += 1
    return hits / len(expected)


def false_positive_rate(
    predicted_status: Sequence[str],
    true_covered: Sequence[bool],
    gap_values: tuple[str, ...] = ("gap", "partial"),
) -> float:
    """Coverage-check false-positive rate (spec 1.5).

    ``FP rate = (truly covered AND flagged) / (truly covered)``.
    """
    if len(predicted_status) != len(true_covered):
        raise ValueError(
            f"length mismatch: {len(predicted_status)} statuses "
            f"vs {len(true_covered)} truth flags"
        )

    covered_total = sum(1 for c in true_covered if c)
    if covered_total == 0:
        return 0.0

    false_positives = sum(
        1
        for status, covered in zip(predicted_status, true_covered, strict=False)
        if covered and status in gap_values
    )
    return false_positives / covered_total
