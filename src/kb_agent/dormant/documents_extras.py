"""Dormant document helpers moved out of the active document track.

DORMANT: ``recall_at_5_from_golden`` is the recall@k baseline, used only by the
(dormant) ``kb_retrieve`` stage and the evals harness. The active KB-only
workflow has no retrieval-check stage, so this is not wired in. Moved verbatim
from ``documents/knowledge_base.py`` in the v3 KB-only reorg. It imports the
still-active ``KnowledgeBaseStage``.
"""

from __future__ import annotations

from kb_agent.documents.knowledge_base import KnowledgeBaseStage


def recall_at_5_from_golden(
    stage: KnowledgeBaseStage,
    golden: list[dict],
    k: int = 5,
) -> tuple[float, list[dict]]:
    """Run the golden set and compute recall@k (spec 1.5 baseline).

    Each golden row has ``question`` and ``expected_source_ids``. A hit means any
    expected id appears (as a substring of a retrieved source id) within top-k -
    substring because the KB reports chunk-level source ids that embed the
    document id.

    Returns ``(recall, per_question_detail)``.

    Recall@k is computed inline (a hit = any expected id appears as a substring of
    a retrieved source id within top-k) so the runtime pipeline does not depend on
    the ``evals`` package - evals depends on ``src``, not the reverse.
    """
    detail: list[dict] = []
    hits = 0

    for row in golden:
        passages = stage.retrieve(row["question"], top_k=k)
        got = [p.source_id for p in passages]
        expected = row["expected_source_ids"]
        hit = any(any(exp in g for g in got) for exp in expected)
        if hit:
            hits += 1
        detail.append(
            {"id": row.get("id"), "hit": hit, "retrieved": got[:k], "expected": expected}
        )

    recall = hits / len(golden) if golden else 0.0
    return recall, detail
