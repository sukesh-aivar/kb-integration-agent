"""DORMANT pipeline stage methods (ticket track + join + kb_retrieve).

Option B-mixin (v3 KB-only reorg): the nine dormant ``_stage_*`` methods were
moved here VERBATIM from ``Pipeline`` so the active ``pipeline.py`` reads as the
four KB-only stages. The active ``Pipeline`` does NOT inherit this mixin, so these
methods are never dispatched in the active workflow. They still use ``self.*``
(``self._clients``, ``self._config``, ``self._rs``, ``self._write_state``, etc.),
which resolve when a dormant test composes a ``Pipeline`` subclass that inherits
this mixin.

To re-enable the ticket/join workflow:
    class Pipeline(DormantStagesMixin, ...): ...
and restore ``LEGACY_STAGE_ORDER_13`` as the active order.

Also hosts ``_silhouette_excluding_noise`` (used only by ``_stage_intent_cluster``),
which is the sole reason the active pipeline imported numpy + scikit-learn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kb_agent.contract import AgentInput
from kb_agent.dormant.documents_extras import recall_at_5_from_golden
from kb_agent.dormant.join.coverage import coverage_check
from kb_agent.dormant.join.drafting import draft_articles
from kb_agent.dormant.tickets.adapters.sample import SampleAdapter
from kb_agent.dormant.tickets.clean import clean_tickets, embedding_texts
from kb_agent.dormant.tickets.cluster import (
    cluster_embeddings,
    should_use_density_clustering,
)
from kb_agent.dormant.tickets.embed import EmbeddingCache
from kb_agent.dormant.tickets.loaders import load_tickets_uri
from kb_agent.dormant.tickets.naming import name_clusters
from kb_agent.logging_setup import get_logger
from kb_agent.obs.audit import RunManifest

logger = get_logger(__name__)


class DormantStagesMixin:
    """The nine dormant ``_stage_*`` methods, moved verbatim (ticket track + join).

    These methods call ``self._clients``/``self._config``/etc., which are provided
    by ``Pipeline`` when a dormant test composes ``Pipeline`` + this mixin. The
    annotations below exist only so the type checker knows those attributes are
    supplied by the host class; the mixin never sets them.
    """

    if TYPE_CHECKING:  # pragma: no cover - typing only
        _clients: Any
        _config: Any

    # --- blue pill stages ----------------------------------------------------

    def _stage_load_tickets(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if not ai.ticket_uri:
            logger.info("no_tickets_supplied")
            state._raw_rows = []  # type: ignore[attr-defined]
            return
        rows = load_tickets_uri(ai.ticket_uri, self._clients.store)
        state._raw_rows = rows  # type: ignore[attr-defined]

    def _stage_adapt_tickets(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        rows = getattr(state, "_raw_rows", [])
        tickets, report = SampleAdapter().adapt(rows)
        state.tickets = tickets
        state.metrics.ticket_count = len(tickets)

    def _stage_clean_tickets(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        state.tickets, _ = clean_tickets(state.tickets)

    def _stage_embed_texts(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        cache = EmbeddingCache()
        state._vectors = cache.get_or_compute(  # type: ignore[attr-defined]
            embedding_texts(state.tickets), self._clients.embeddings
        )

    def _stage_intent_cluster(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        vectors = getattr(state, "_vectors", [])
        if not vectors:
            return
        if should_use_density_clustering(len(state.tickets), self._config):
            result = cluster_embeddings(vectors, self._config)
            state.labels = result.labels
            state.reduced = result.reduced
            state.metrics.cluster_count = result.n_clusters
            state.metrics.noise_share = round(result.noise_share, 4)
            # Silhouette on the reduced coordinates, excluding noise (spec 1.5).
            state.metrics.silhouette = _silhouette_excluding_noise(result.reduced, result.labels)
        else:
            # <500 path handled in name step via LLM categorisation fallback.
            state.labels = [0] * len(state.tickets)
            import numpy as np

            state.reduced = np.zeros((len(state.tickets), 2))

    def _stage_name_clusters(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if not state.labels:
            return
        clusters, pv = name_clusters(
            state.tickets, state.labels, state.reduced, self._clients.llm,
            seed=self._config.seed,
        )
        state.clusters = clusters
        manifest.record_model_call(self._clients.llm.model_id, "naming", pv)

    # --- kb_retrieve (dormant: needs a golden question set) ------------------

    def _stage_kb_retrieve(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if state.kb_stage and golden:
            recall, _ = recall_at_5_from_golden(state.kb_stage, golden)
            state.metrics.recall_at_5 = round(recall, 4)

    # --- join stages ---------------------------------------------------------

    def _stage_coverage_check(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if not (state.kb_stage and state.clusters):
            return
        tickets_by_id = {t.ticket_id: t for t in state.tickets}
        items, pv = coverage_check(
            state.clusters, tickets_by_id, state.kb_stage, self._clients.llm, self._config
        )
        state.coverage = items
        from kb_agent.models import CoverageStatus

        state.metrics.gaps_flagged = sum(1 for i in items if i.status == CoverageStatus.GAP)
        manifest.record_model_call(self._clients.llm.model_id, "coverage", pv)

    def _stage_draft_article(
        self,
        ai: AgentInput,
        state: Any,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if not state.clusters:
            return
        # Drafting is gated on the coverage result: only gaps/partials are drafted
        # (spec 4.4). state.coverage is populated by the coverage_check stage,
        # which runs before this one in STAGE_ORDER.
        outcomes, pv = draft_articles(
            state.clusters, state.labels, state.tickets, state.coverage,
            run_id=ai.run_id,
            llm=self._clients.llm, guardrail=self._clients.guardrail,
            store=self._clients.store, config=self._config,
        )
        state.drafts = outcomes
        manifest.record_model_call(self._clients.llm.model_id, "drafting", pv)


def _silhouette_excluding_noise(reduced: Any, labels: list[int]) -> float | None:
    """Silhouette score over clustered points only (noise -1 excluded; spec 1.5).

    Returns None when silhouette is undefined (fewer than 2 clusters remain after
    dropping noise), so the report shows 'n/a' rather than a misleading number.
    Computed directly via sklearn so the runtime pipeline does not depend on the
    ``evals`` package (evals depends on src, not the reverse).
    """
    import numpy as np
    from sklearn.metrics import silhouette_score

    arr = np.asarray(reduced)
    mask = np.array([lbl != -1 for lbl in labels])
    kept_labels = [lbl for lbl in labels if lbl != -1]
    if len({*kept_labels}) < 2 or len(kept_labels) <= len({*kept_labels}):
        return None
    return round(float(silhouette_score(arr[mask], kept_labels, metric="euclidean")), 4)
