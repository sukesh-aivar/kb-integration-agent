"""End-to-end pipeline orchestration (spec 4.1, 3 resumability, 5 observability).

Wires the red pill, blue pill and join into one resumable, timed, audited run that
is identical in fake and aws modes (the only difference is the injected
:class:`ClientSet`). Design points from the spec:

* **Stateless/idempotent/resumable (spec 3):** each completed stage writes a
  completion marker to the object store; on re-entry a completed stage is skipped
  unless forced. A ``run()`` (synchronous, for local testing) and a stage-wise
  ``step()`` are both supported via ``resume_from`` (spec 3 / contract).
* **Timed + audited (spec 5):** every stage is wrapped in a timer and an OTEL
  span, and facts land in the run manifest.
* **Draft-until-approved (spec 2.2):** articles only ever go to ``drafts/{run_id}/``.
* **No ground-truth leak (spec 2.4):** only subject+description reaches embeddings.

This module contains orchestration only; the per-stage logic lives in the stage
modules (spec 7.4 "no business logic in the orchestrator" is applied to the
entrypoint; the pipeline sequences the stages but delegates all work).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kb_agent.aws.clientset import ClientSet
from kb_agent.config import AppConfig
from kb_agent.contract import AgentInput, AgentOutput, ArtifactRef, RunMetrics
from kb_agent.documents.knowledge_base import KnowledgeBaseStage, recall_at_5_from_golden
from kb_agent.documents.normalize import normalize_raw_documents
from kb_agent.documents.staging import is_stageable_document
from kb_agent.join.coverage import coverage_check
from kb_agent.join.drafting import draft_articles
from kb_agent.join.report import build_report, render_markdown
from kb_agent.logging_setup import bind_run_id, get_logger
from kb_agent.models import Route, SourceType
from kb_agent.obs.audit import RunManifest, library_versions
from kb_agent.obs.timing import StageTimer
from kb_agent.obs.tracing import stage_span
from kb_agent.review.state import ReviewRegistry
from kb_agent.tickets.adapters.sample import SampleAdapter
from kb_agent.tickets.clean import clean_tickets, embedding_texts
from kb_agent.tickets.cluster import cluster_embeddings, should_use_density_clustering
from kb_agent.tickets.embed import EmbeddingCache
from kb_agent.tickets.loaders import load_tickets_uri
from kb_agent.tickets.naming import name_clusters

logger = get_logger(__name__)

# The fixed happy-path stage order (spec 6: order of the happy path is CODE).
STAGE_ORDER = [
    "load_tickets",
    "adapt_tickets",
    "clean_tickets",
    "embed_texts",
    "intent_cluster",
    "name_clusters",
    "normalize_docs",
    "kb_create",
    "kb_ingest",
    "kb_retrieve",
    "coverage_check",
    "draft_article",
    "write_report",
]


@dataclass
class PipelineState:
    """Mutable state threaded through the stages of one run."""

    tickets: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    reduced: Any = None
    clusters: list = field(default_factory=list)
    normalized_docs: list = field(default_factory=list)
    kb_stage: KnowledgeBaseStage | None = None
    coverage: list = field(default_factory=list)
    drafts: list = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)


class Pipeline:
    """Resumable orchestration of all stages for one run."""

    def __init__(self, clients: ClientSet, config: AppConfig) -> None:
        self._clients = clients
        self._config = config

    # --- resumability helpers ------------------------------------------------

    def _marker_key(self, run_id: str, stage: str) -> str:
        return f"runs/{run_id}/_markers/{stage}.done"

    def _is_done(self, run_id: str, stage: str) -> bool:
        return self._clients.store.exists(self._marker_key(run_id, stage))

    def _mark_done(self, run_id: str, stage: str) -> None:
        self._clients.store.put_text(self._marker_key(run_id, stage), "done")

    # --- public entry points -------------------------------------------------

    def run(self, agent_input: AgentInput, golden: list[dict] | None = None) -> AgentOutput:
        """Run the full pipeline synchronously (local testing path; spec 3)."""
        bind_run_id(agent_input.run_id)
        manifest = RunManifest(
            run_id=agent_input.run_id,
            config_snapshot=self._config.model_dump(mode="json"),
            library_versions=library_versions(),
        )
        timer = StageTimer()
        state = PipelineState()

        start_index = 0
        if agent_input.resume_from:
            if agent_input.resume_from not in STAGE_ORDER:
                raise ValueError(f"unknown resume_from stage: {agent_input.resume_from}")
            start_index = STAGE_ORDER.index(agent_input.resume_from)

        try:
            for stage in STAGE_ORDER[start_index:]:
                if self._is_done(agent_input.run_id, stage) and not agent_input.resume_from:
                    logger.info("stage_skip_completed", stage=stage)
                    manifest.record_stage(stage, "skipped")
                    continue
                with timer.measure(stage), stage_span(stage, agent_input.run_id):
                    self._run_stage(stage, agent_input, state, manifest, golden)
                self._mark_done(agent_input.run_id, stage)
                manifest.record_stage(stage, "completed", timer.durations.get(stage))
        except Exception as exc:  # noqa: BLE001 - record then re-raise as failed output
            logger.error("pipeline_failed", stage=stage, error=str(exc))
            manifest.errors.append({"stage": stage, "error": str(exc)})
            manifest.finish()
            self._write_manifest(agent_input.run_id, manifest)
            return AgentOutput(
                run_id=agent_input.run_id, status="failed", stage=stage,
                metrics=state.metrics,
                errors=[{"stage": stage, "kind": type(exc).__name__, "message": str(exc)}],  # type: ignore[list-item]
            )

        state.metrics.stage_durations_sec = timer.durations
        artifacts = self._finalise(agent_input.run_id, state, manifest)
        manifest.metrics = state.metrics.model_dump(mode="json")
        manifest.finish()
        self._write_manifest(agent_input.run_id, manifest)

        return AgentOutput(
            run_id=agent_input.run_id,
            status="needs_review",  # everything is pending_review (spec 2.2)
            stage="write_report",
            artifacts=artifacts,
            metrics=state.metrics,
            pending_review=[
                ArtifactRef(name="report", uri=artifacts.get("report", ""), status="pending_review")
            ],
        )

    # --- stage dispatch ------------------------------------------------------

    def _run_stage(
        self,
        stage: str,
        agent_input: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        handler = getattr(self, f"_stage_{stage}")
        handler(agent_input, state, manifest, golden)

    # --- blue pill stages ----------------------------------------------------

    def _stage_load_tickets(
        self,
        ai: AgentInput,
        state: PipelineState,
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
        state: PipelineState,
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
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        state.tickets, _ = clean_tickets(state.tickets)

    def _stage_embed_texts(
        self,
        ai: AgentInput,
        state: PipelineState,
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
        state: PipelineState,
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
        state: PipelineState,
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

    # --- red pill stages -----------------------------------------------------

    def _stage_normalize_docs(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        # Path A (freq-test): copy the RAW bytes of each document to the KB
        # data-source prefix so Bedrock's own parser ingests the original file
        # (e.g. a PDF stays a PDF). This replaces the earlier text-only path that
        # UTF-8-decoded and silently skipped binary files like PDFs.
        docs: list[tuple[str, bytes, Route]] = []
        for uri in ai.document_uris:
            key = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri
            name = key.rsplit("/", 1)[-1]
            # Defensive: never ingest non-content files (README, licences, etc.)
            # even if the orchestrator passes them (spec 4.2).
            if not is_stageable_document(name):
                logger.info("skip_non_content_document", name=name)
                continue
            try:
                data = self._clients.store.get_bytes(key)
            except KeyError:
                logger.info("document_not_found", key=key)
                continue
            docs.append((name, data, Route.DEFAULT))
        state.normalized_docs = normalize_raw_documents(
            docs, run_id=ai.run_id, store=self._clients.store,
            data_source_prefix=f"runs/{ai.run_id}/kb-docs",
            source_type=SourceType.UPLOADED_DOC,
        )
        state.metrics.document_count = len(state.normalized_docs)

    def _stage_kb_create(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        stage = KnowledgeBaseStage(self._clients.knowledge_base, self._config)
        stage.create(f"kb-{ai.run_id}", f"runs/{ai.run_id}/kb-docs")
        state.kb_stage = stage

    def _stage_kb_ingest(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if state.kb_stage:
            state.kb_stage.ingest()

    def _stage_kb_retrieve(
        self,
        ai: AgentInput,
        state: PipelineState,
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
        state: PipelineState,
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
        state: PipelineState,
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

    def _stage_write_report(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        report = build_report(
            run_id=ai.run_id,
            kb_id=state.kb_stage.kb_id if state.kb_stage else None,
            metrics=state.metrics,
            clusters=state.clusters,
            coverage=state.coverage,
            drafts=state.drafts,
            mode=self._clients.mode,
        )
        state._report = report  # type: ignore[attr-defined]

    # --- finalisation --------------------------------------------------------

    def _finalise(self, run_id: str, state: PipelineState, manifest: RunManifest) -> dict[str, str]:
        """Write the report (MD+JSON) and register review state (spec 2.2, 4.4)."""
        registry = ReviewRegistry()
        report = getattr(state, "_report", None)
        artifacts: dict[str, str] = {}

        if report is not None:
            json_uri = self._clients.store.put_text(
                f"runs/{run_id}/report.json", json.dumps(report, indent=2)
            )
            md_uri = self._clients.store.put_text(
                f"runs/{run_id}/report.md", render_markdown(report)
            )
            artifacts["report"] = md_uri
            artifacts["report_json"] = json_uri
            registry.register("report", "report", uri=md_uri)

        for outcome in state.drafts:
            if outcome.drafted and outcome.article:
                registry.register(
                    outcome.article.article_id, "article",
                    model_id=outcome.article.model_id,
                    prompt_version=outcome.article.prompt_version,
                )

        manifest.review_records = registry.to_records()
        manifest.artifacts = artifacts
        return artifacts

    def _write_manifest(self, run_id: str, manifest: RunManifest) -> None:
        self._clients.store.put_text(
            f"runs/{run_id}/manifest.json", manifest.model_dump_json(indent=2)
        )


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
