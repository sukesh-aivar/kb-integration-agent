"""Active eval metrics (v3 KB-only reorg).

Only ``routing_accuracy`` remains active — it scores the document router against
``routing_fixtures.yaml`` and belongs to the KB-only document track. The dormant
clustering/retrieval/coverage metrics (intent_purity, silhouette, cluster_count,
noise_share, recall_at_k, false_positive_rate) moved verbatim to
``dormant/evals/metrics.py`` with the ticket/join workflow.

``routing_accuracy`` is pure: no AWS, no network, no file I/O (spec 7.4).
"""

from __future__ import annotations

from collections.abc import Sequence


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
