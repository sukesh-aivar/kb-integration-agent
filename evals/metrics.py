"""Scoring metrics for the Week 1 validation targets (spec 1.5, build step 3).

Every function here is **pure**: no AWS, no network, no file I/O. That is a
deliberate spec requirement (7.4) so the maths is unit-testable offline and the
``make test`` suite needs no credentials.

Metrics implemented:

* ``intent_purity`` - how well discovered clusters match the seeded intents
  (target >= 0.80). Measures the pipeline's clustering, scored against the hidden
  ``labels.csv`` ground truth that never entered the pipeline (spec 2.4).
* ``silhouette`` - thin wrapper over scikit-learn's silhouette score, for the
  over-splitting / merging diagnostic (spec 1.5 added metric).
* ``recall_at_k`` - fraction of golden-set questions whose known-correct source
  appears in the top-k retrieved results (target >= 0.80).
* ``false_positive_rate`` - share of truly-covered intents the coverage check
  wrongly flagged as a gap (spec 1.5: guards against "flag everything").

The purity and recall implementations are standalone (no sklearn needed) so they
can be reasoned about and tested in isolation; silhouette defers to sklearn.
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

    Args:
        predicted_labels: Cluster id per item, as produced by the pipeline.
        true_labels: Seeded intent per item (ground truth, from ``labels.csv``).
        ignore_label: A predicted label to exclude from scoring - by default the
            HDBSCAN noise bucket ``-1``, which is unassigned and would unfairly
            depress purity. Pass ``None`` to score everything.

    Returns:
        Purity in ``[0.0, 1.0]``. Returns ``0.0`` if there is nothing to score.

    Raises:
        ValueError: If the two label sequences differ in length.
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
        # Majority true label in this cluster is the count of its most common item.
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
    """Silhouette score of a clustering (spec 1.5 over-split/merge diagnostic).

    Thin wrapper over scikit-learn. Noise points (label ``-1``) and clusters are
    passed through as-is; the caller decides whether to filter noise first.
    sklearn requires at least 2 clusters and fewer clusters than samples.

    Args:
        embeddings: Array-like of shape ``(n_samples, n_features)``.
        predicted_labels: Cluster id per sample.
        metric: Distance metric (``"cosine"`` for text embeddings).

    Returns:
        Mean silhouette in ``[-1.0, 1.0]``, or ``0.0`` if it is undefined
        (fewer than 2 clusters, or every point in its own cluster).
    """
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
    """Recall@k over a golden retrieval set (spec 1.5, target >= 0.80).

    For each question, success means the known-correct source id appears within
    the first ``k`` retrieved ids. Recall@k is the fraction of questions that
    succeed.

    Args:
        retrieved: Per-question ordered list of retrieved source ids
            (most relevant first).
        expected: Per-question known-correct source id.
        k: Cut-off rank.

    Returns:
        Recall@k in ``[0.0, 1.0]``.

    Raises:
        ValueError: If ``retrieved`` and ``expected`` differ in length, or k < 1.
    """
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

    A false positive is an intent that is *truly covered* by the knowledge base
    but which the coverage check flagged as a gap (or partial). The FP rate is
    the share of truly-covered intents that were wrongly flagged. It guards
    against trivially hitting the "3 of 3 gaps flagged" target by flagging
    everything.

    ``FP rate = (truly covered AND flagged) / (truly covered)``

    Args:
        predicted_status: Coverage status per intent (``"covered" | "partial" |
            "gap"``).
        true_covered: Ground truth - is each intent actually covered?
        gap_values: Which predicted statuses count as "flagged as a gap".

    Returns:
        False-positive rate in ``[0.0, 1.0]``; ``0.0`` if no intent is truly
        covered (nothing could be a false positive).

    Raises:
        ValueError: If the two sequences differ in length.
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


def routing_accuracy(
    predicted_routes: Sequence[str],
    expected_routes: Sequence[str],
) -> float:
    """Fraction of files routed to the expected route (spec 1.5 added metric).

    Scored against ``routing_fixtures.yaml``.
    """
    if len(predicted_routes) != len(expected_routes):
        raise ValueError(
            f"length mismatch: {len(predicted_routes)} predicted "
            f"vs {len(expected_routes)} expected"
        )
    if not expected_routes:
        return 0.0
    correct = sum(1 for p, e in zip(predicted_routes, expected_routes, strict=False) if p == e)
    return correct / len(expected_routes)
