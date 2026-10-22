"""kb_create / kb_ingest / kb_retrieve (spec 4.2).

Thin orchestration over the injected :class:`KnowledgeBaseClient`. The verified
API shape (Part A item 2) is: create KB + S3 data source with a chunking strategy,
start an async ingestion job, poll it to completion, then retrieve. All of that
lives behind the client protocol so this stage runs identically in fake and aws
modes.

``kb_retrieve`` plus the golden set gives the recall@5 baseline (spec 1.5).
"""

from __future__ import annotations

from kb_agent.aws.interfaces import KnowledgeBaseClient, RetrievedPassage
from kb_agent.config import AppConfig
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


class KnowledgeBaseStage:
    """Create, ingest into, and query a knowledge base."""

    def __init__(self, client: KnowledgeBaseClient, config: AppConfig) -> None:
        self._client = client
        self._config = config
        self._kb_id: str | None = None

    @property
    def kb_id(self) -> str | None:
        return self._kb_id

    @property
    def data_source_id(self) -> str | None:
        """The S3 data source id, needed for the chunk sample (spec v2 §5.6).

        Only the real client tracks this; fakes may not expose it. Returns None
        when the underlying client does not carry one.
        """
        return getattr(self._client, "data_source_id", None)

    def create(self, name: str, data_source_prefix: str) -> str:
        """Create (or reuse) the KB with the configured chunking strategy."""
        strategy = self._config.knowledge_base.chunking_strategy
        self._kb_id = self._client.create(name, data_source_prefix, strategy)
        logger.info("kb_created", kb_id=self._kb_id, chunking=strategy)
        return self._kb_id

    def ingest(self) -> dict | str:
        """Start ingestion and wait for completion (polled; Part A item 2).

        Returns the client's document-level ingestion statistics dict (spec v2
        §5.5) when the client exposes them (``last_ingestion_stats``); otherwise
        falls back to the bare status string (keeps fakes/older clients working).
        """
        if self._kb_id is None:
            raise RuntimeError("create() must run before ingest()")
        job_id = self._client.start_ingestion(self._kb_id)
        status = self._client.wait_for_ingestion(self._kb_id, job_id)
        logger.info("kb_ingested", kb_id=self._kb_id, job_id=job_id, status=status)
        stats = getattr(self._client, "last_ingestion_stats", None)
        return stats if isinstance(stats, dict) else status

    def sample_chunks(self, run_id: str, max_chunks: int = 5) -> dict:
        """Precompute a small chunk sample for this run (spec v2 §5.6).

        Delegates to the client when it supports sampling; otherwise returns an
        empty-but-valid sample. A missing sample must never fail the run.
        """
        sampler = getattr(self._client, "sample_chunks", None)
        if callable(sampler):
            return sampler(run_id, max_chunks)
        return {"chunks": [], "error": "unavailable"}

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedPassage]:
        """Retrieve the top-k passages for a query."""
        if self._kb_id is None:
            raise RuntimeError("create() must run before retrieve()")
        k = top_k if top_k is not None else self._config.knowledge_base.retrieval_top_k
        return self._client.retrieve(self._kb_id, query, k)


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
