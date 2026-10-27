"""intent_cluster: UMAP + HDBSCAN density clustering (spec 4.3).

Why reduce dimensions at all: embeddings are high-dimensional; in very high
dimensions the gap between nearest and farthest neighbour shrinks, so the density
contrast HDBSCAN relies on fades. UMAP preserves local neighbourhood structure
while discarding much of the phrasing/length/style noise. ``n_components`` is the
number of AXES (default 10, range 5-15) and has nothing to do with the number of
intents - do NOT set it to 20 (spec 4.3).

HDBSCAN is used because it does not need the cluster count up front, handles very
uneven cluster sizes, and labels sparse points as noise (-1) rather than forcing
them into a cluster - which is exactly where the seeded non-English/vague tickets
should land. K-means was rejected (needs k, assumes round equal clusters, assigns
everything). This is deterministic given a fixed seed (spec 2.3): random_state is
set from config and exposed.

Below the configured threshold (default 500) density clustering is unstable, so
the caller falls back to LLM categorisation (categorise.py) - that decision is
CODE, not a model (spec 2.1), and lives in :func:`should_use_density_clustering`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from kb_agent.config import AppConfig
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ClusterResult:
    """Output of density clustering: a label per ticket plus diagnostics."""

    labels: list[int]  # cluster id per ticket; -1 == noise
    n_clusters: int  # excluding noise
    noise_share: float
    reduced: np.ndarray  # the UMAP-reduced coordinates (for silhouette/plots)


def should_use_density_clustering(n_tickets: int, config: AppConfig) -> bool:
    """Decide clustering vs LLM categorisation by row count (spec 4.3).

    This is a deterministic threshold check, not a model call (spec 2.1). 500 is
    a heuristic in config, not a law.
    """
    return n_tickets >= config.clustering.cluster_threshold


def cluster_embeddings(embeddings: list[list[float]], config: AppConfig) -> ClusterResult:
    """Reduce with UMAP then cluster with HDBSCAN, both seeded (spec 4.3).

    Args:
        embeddings: One vector per ticket.
        config: Supplies seed, UMAP n_components/metric, HDBSCAN min_cluster_size.

    Returns:
        A :class:`ClusterResult`.
    """
    import hdbscan
    import umap

    x = np.asarray(embeddings, dtype=np.float64)
    n = x.shape[0]

    # n_neighbors must be < n_samples; clamp for small inputs so tests on tiny
    # fixtures do not crash (production runs are ~1,500 tickets).
    n_neighbors = min(15, max(2, n - 1))
    n_components = min(config.clustering.umap.n_components, max(2, n - 2))

    reducer = umap.UMAP(
        n_components=n_components,
        n_neighbors=n_neighbors,
        metric=config.clustering.umap.metric,
        random_state=config.seed,  # fixed seed -> reproducible (spec 2.3)
    )
    reduced = reducer.fit_transform(x)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=config.clustering.hdbscan.min_cluster_size,
        metric="euclidean",  # UMAP output is Euclidean-friendly
    )
    labels = clusterer.fit_predict(reduced).tolist()

    n_clusters = len({label for label in labels if label != -1})
    noise = sum(1 for label in labels if label == -1)
    noise_share = noise / n if n else 0.0

    logger.info(
        "cluster_complete",
        tickets=n,
        clusters=n_clusters,
        noise_share=round(noise_share, 4),
        n_components=n_components,
    )
    return ClusterResult(
        labels=labels, n_clusters=n_clusters, noise_share=noise_share, reduced=reduced
    )
