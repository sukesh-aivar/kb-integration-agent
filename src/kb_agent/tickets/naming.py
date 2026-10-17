"""name_clusters: turn clusters into named intents with volumes (spec 4.3).

One LLM call per cluster (~20 per run, not 1,500) - permitted LLM use #1 (spec
2.1). The prompt input is a SAMPLE of each cluster: the most central tickets plus
some random members, so a mixed cluster is visible rather than hidden behind its
centroid. The model returns structured output including a ``coherence_flag`` so it
can say "this cluster mixes several intents", routing it to a reviewer for a split.

Every ticket in a cluster inherits that cluster's label, so cluster impurity
propagates into volume counts and drafted articles - which is exactly why the
review gate sits before drafting (spec 4.3). This stage therefore also computes
the volumes and resolved share that the intent view and drafting rely on.

A post-step flags near-identical cluster names as merge candidates.
"""

from __future__ import annotations

import json
import random

import numpy as np

from kb_agent.aws.interfaces import LlmClient
from kb_agent.logging_setup import get_logger
from kb_agent.models import IntentCluster, Ticket
from kb_agent.prompts import prompt_version, render_prompt

logger = get_logger(__name__)


def _cluster_members(labels: list[int]) -> dict[int, list[int]]:
    """Group ticket indices by cluster id (noise -1 included)."""
    members: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        members.setdefault(label, []).append(idx)
    return members


def _sample_indices(
    indices: list[int], reduced: np.ndarray, k: int, seed: int
) -> list[int]:
    """Pick the k/2 most central + k/2 random members of a cluster (spec 4.3)."""
    if len(indices) <= k:
        return list(indices)
    pts = reduced[indices]
    centroid = pts.mean(axis=0)
    dists = np.linalg.norm(pts - centroid, axis=1)
    order = np.argsort(dists)
    n_central = max(1, k // 2)
    central = [indices[i] for i in order[:n_central]]
    rng = random.Random(seed)
    remaining = [i for i in indices if i not in central]
    rng.shuffle(remaining)
    randoms = remaining[: k - len(central)]
    return central + randoms


def name_clusters(
    tickets: list[Ticket],
    labels: list[int],
    reduced: np.ndarray,
    llm: LlmClient,
    *,
    seed: int,
    sample_size: int = 8,
    version: str = "v1",
) -> tuple[list[IntentCluster], str]:
    """Name every non-noise cluster and compute its volume/resolved share.

    Returns ``(clusters, prompt_version_id)``. Clusters are sorted by volume
    descending. The noise bucket (-1) is returned as a cluster named "noise".
    """
    members = _cluster_members(labels)
    clusters: list[IntentCluster] = []

    for cluster_id, idxs in sorted(members.items()):
        volume = len(idxs)
        resolved = sum(1 for i in idxs if tickets[i].resolution)
        resolved_share = resolved / volume if volume else 0.0

        if cluster_id == -1:
            clusters.append(
                IntentCluster(
                    cluster_id=-1,
                    intent_name="noise",
                    description="Unclustered / sparse tickets (non-English, vague, outliers).",
                    volume=volume,
                    resolved_share=round(resolved_share, 4),
                    example_ticket_ids=[tickets[i].ticket_id for i in idxs[:3]],
                    coherence_flag=False,
                )
            )
            continue

        sample_idx = _sample_indices(idxs, reduced, sample_size, seed + cluster_id)
        tickets_block = "\n\n".join(
            f"[{tickets[i].ticket_id}] {tickets[i].embedding_text()}" for i in sample_idx
        )
        prompt = render_prompt("naming", version, tickets_block=tickets_block)
        raw = llm.invoke(prompt, max_tokens=256)
        parsed = _parse_naming(raw)

        clusters.append(
            IntentCluster(
                cluster_id=cluster_id,
                intent_name=parsed["intent_name"],
                description=parsed["description"],
                volume=volume,
                resolved_share=round(resolved_share, 4),
                example_ticket_ids=[tickets[i].ticket_id for i in sample_idx[:3]],
                coherence_flag=bool(parsed["coherence_flag"]),
            )
        )

    clusters.sort(key=lambda c: c.volume, reverse=True)
    _flag_merge_candidates(clusters)
    logger.info("naming_complete", clusters=len(clusters))
    return clusters, prompt_version("naming", version)


def _parse_naming(raw: str) -> dict:
    """Parse+validate the naming JSON, with safe defaults on malformed output."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "intent_name": "unparsed_cluster",
            "description": "Model output could not be parsed.",
            "coherence_flag": True,  # force reviewer attention
        }
    return {
        "intent_name": str(obj.get("intent_name", "unnamed")),
        "description": str(obj.get("description", "")),
        "coherence_flag": bool(obj.get("coherence_flag", False)),
    }


def _flag_merge_candidates(clusters: list[IntentCluster]) -> None:
    """Flag near-identical intent names as merge candidates (spec 4.3 post-step).

    Marks coherence_flag on clusters that share a normalised name, so the reviewer
    sees them as merge candidates in the intent view.
    """
    seen: dict[str, IntentCluster] = {}
    for c in clusters:
        if c.cluster_id == -1:
            continue
        norm = c.intent_name.strip().lower().replace("-", "_").replace(" ", "_")
        if norm in seen:
            c.coherence_flag = True
            seen[norm].coherence_flag = True
        else:
            seen[norm] = c
