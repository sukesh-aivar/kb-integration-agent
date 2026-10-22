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
from kb_agent.documents.bda import BdaProcessor
from kb_agent.documents.features import extract_features
from kb_agent.documents.knowledge_base import KnowledgeBaseStage, recall_at_5_from_golden
from kb_agent.documents.normalize import normalize_documents, normalize_raw_documents
from kb_agent.documents.router import route_document
from kb_agent.documents.staging import is_stageable_document
from kb_agent.join.coverage import coverage_check
from kb_agent.join.drafting import draft_articles
from kb_agent.join.report import build_report, render_markdown
from kb_agent.logging_setup import bind_run_id, get_logger
from kb_agent.models import Route, SourceType
from kb_agent.obs.audit import RunManifest, library_versions
from kb_agent.obs.runstate import RunStateWriter, compute_skip_set
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


# Reader-facing stage labels (match the frontend STEP labels). Used only for the
# run-state event text; stage IDs remain the canonical identifiers.
STAGE_LABELS = {
    "load_tickets": "Reading the ticket export",
    "adapt_tickets": "Mapping ticket fields",
    "clean_tickets": "Cleaning and de-duplicating",
    "embed_texts": "Embedding ticket text",
    "intent_cluster": "Discovering intents",
    "name_clusters": "Naming intents",
    "normalize_docs": "Reading and parsing documents",
    "kb_create": "Creating the knowledge base",
    "kb_ingest": "Indexing content",
    "kb_retrieve": "Checking retrieval",
    "coverage_check": "Checking coverage",
    "draft_article": "Drafting articles",
    "write_report": "Writing the report",
}


def _utcnow_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _store_uri(store: Any, key: str) -> str:
    """Build a full ``s3://bucket/key`` URI for a bare key via the store.

    Both the real S3 store and the fake expose a private ``_uri(key)`` that
    prepends their bucket. We use it defensively: if a store does not expose it
    (or it raises), fall back to the bare key so offline/fake paths still work.
    Never build ``f"s3://{key}"`` directly — that drops the bucket name and makes
    Bedrock Data Automation read from a bucket named after the first key segment.
    """
    uri_fn = getattr(store, "_uri", None)
    if callable(uri_fn):
        try:
            return uri_fn(key)
        except Exception:  # noqa: BLE001 - defensive; fall back to the key
            pass
    return key


def _error_kind(exc: Exception) -> str:
    """Map an exception to a stable error kind for _finished.json (spec v2 §5.3)."""
    from kb_agent.exceptions import (
        IngestionError,
        PageCapExceededError,
        ParsingError,
        ValidationError,
    )
    if isinstance(exc, PageCapExceededError):
        return "page_cap_exceeded"
    if isinstance(exc, ValidationError):
        return "validation_error"
    if isinstance(exc, ParsingError):
        return "parsing_error"
    if isinstance(exc, IngestionError):
        return "ingestion_error"
    return "agent_error"  # catch-all


def _plain_error(exc: Exception) -> str:
    """A plain-English message with no stack trace (spec v2 §5.3)."""
    return str(exc) or exc.__class__.__name__


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

    def __init__(self, clients: ClientSet, config: AppConfig, *, force_bda: bool = False) -> None:
        self._clients = clients
        self._config = config
        self._force_bda = force_bda  # force every document down the BDA route (test)
        self._rs: RunStateWriter | None = None  # set per run() when enabled

    # --- resumability helpers ------------------------------------------------

    def _marker_key(self, run_id: str, stage: str) -> str:
        return f"runs/{run_id}/_markers/{stage}.done"

    def _is_done(self, run_id: str, stage: str) -> bool:
        return self._clients.store.exists(self._marker_key(run_id, stage))

    def _mark_done(self, run_id: str, stage: str) -> None:
        self._clients.store.put_text(self._marker_key(run_id, stage), "done")

    # --- public entry points -------------------------------------------------

    def run(
        self,
        agent_input: AgentInput,
        golden: list[dict] | None = None,
        *,
        emit_runstate: bool = False,
    ) -> AgentOutput:
        """Run the full pipeline synchronously (spec 3).

        ``emit_runstate=True`` additionally writes the live progress files the
        backend reads (``_started/_events/_heartbeat/_state/_finished``, spec v2
        §5.3). Default False keeps the CLI and all existing tests byte-for-byte
        identical (no extra S3 writes).
        """
        bind_run_id(agent_input.run_id)
        manifest = RunManifest(
            run_id=agent_input.run_id,
            config_snapshot=self._config.model_dump(mode="json"),
            library_versions=library_versions(),
        )
        timer = StageTimer()
        state = PipelineState()
        state._owner_id = agent_input.owner_id  # type: ignore[attr-defined]
        state._max_pages = agent_input.max_pages  # type: ignore[attr-defined]

        # run-state writer (opt-in). Compute the skip set up front from inputs.
        rs: RunStateWriter | None = None
        skipped = compute_skip_set(
            has_tickets=bool(agent_input.ticket_uri),
            has_golden=bool(golden),
        )
        if emit_runstate:
            rs = RunStateWriter(self._clients.store, agent_input.run_id)
            rs.started(
                stage_order=list(STAGE_ORDER),
                skipped=skipped,
                inputs={
                    "documents": [{"uri": u} for u in agent_input.document_uris],
                    "ticket": agent_input.ticket_uri,
                },
                owner_id=agent_input.owner_id,
            )
            for stg, reason in skipped.items():
                rs.stage_skipped(stg, reason)
            rs.start_heartbeat()
        self._rs = rs

        start_index = 0
        if agent_input.resume_from:
            if agent_input.resume_from not in STAGE_ORDER:
                raise ValueError(f"unknown resume_from stage: {agent_input.resume_from}")
            start_index = STAGE_ORDER.index(agent_input.resume_from)

        stage = STAGE_ORDER[start_index]  # default so the except clause has a value
        try:
            for stage in STAGE_ORDER[start_index:]:
                if self._is_done(agent_input.run_id, stage) and not agent_input.resume_from:
                    logger.info("stage_skip_completed", stage=stage)
                    manifest.record_stage(stage, "skipped")
                    continue
                if stage in skipped:
                    manifest.record_stage(stage, "skipped", output=skipped[stage])
                    continue
                if rs:
                    rs.set_stage(stage)
                    rs.stage_started(stage, STAGE_LABELS.get(stage, stage))
                with timer.measure(stage), stage_span(stage, agent_input.run_id):
                    self._run_stage(stage, agent_input, state, manifest, golden)
                self._mark_done(agent_input.run_id, stage)
                manifest.record_stage(stage, "completed", timer.durations.get(stage))
                if rs:
                    rs.stage_done(stage, STAGE_LABELS.get(stage, stage),
                                  data={"duration_sec": round(timer.durations.get(stage, 0.0), 2)})
        except Exception as exc:  # noqa: BLE001 - record then re-raise as failed output
            logger.error("pipeline_failed", stage=stage, error=str(exc))
            manifest.errors.append({"stage": stage, "error": str(exc)})
            manifest.finish()
            self._write_manifest(agent_input.run_id, manifest)
            if rs:
                rs.stage_failed(stage, _plain_error(exc))
                rs.stop_heartbeat()
                rs.finished({
                    "status": "failed",
                    "ended_at": _utcnow_iso(),
                    "elapsed_seconds": round(sum(timer.durations.values()), 2),
                    "failing_stage": stage,
                    "error": {"kind": _error_kind(exc), "message": _plain_error(exc)},
                })
            return AgentOutput(
                run_id=agent_input.run_id, status="failed", stage=stage,
                metrics=state.metrics,
                errors=[{"stage": stage, "kind": type(exc).__name__, "message": str(exc)}],  # type: ignore[list-item]
            )
        except BaseException as exc:  # noqa: BLE001 - e.g. KeyboardInterrupt/SystemExit
            # A non-Exception interrupt (spec v2 §5.1 item 4): still write a
            # terminal _finished.json so the backend never sees a run hang
            # "running" forever, then RE-RAISE so the caller/thread still sees it.
            logger.error("pipeline_interrupted", stage=stage, error=repr(exc))
            if rs:
                try:
                    rs.stage_failed(stage, "The run stopped unexpectedly.")
                    rs.stop_heartbeat()
                    rs.finished({
                        "status": "failed",
                        "ended_at": _utcnow_iso(),
                        "elapsed_seconds": round(sum(timer.durations.values()), 2),
                        "failing_stage": stage,
                        "error": {"kind": "agent_error",
                                  "message": "The run stopped unexpectedly."},
                    })
                except Exception:  # noqa: BLE001 - never mask the original interrupt
                    logger.error("interrupt_finished_write_failed", stage=stage)
            raise
        finally:
            self._rs = None

        state.metrics.stage_durations_sec = timer.durations
        artifacts = self._finalise(agent_input.run_id, state, manifest)
        manifest.metrics = state.metrics.model_dump(mode="json")
        manifest.finish()
        self._write_manifest(agent_input.run_id, manifest)
        if rs:
            rs.stop_heartbeat()
            rs.finished({
                "status": "needs_review",
                "ended_at": _utcnow_iso(),
                "elapsed_seconds": round(sum(timer.durations.values()), 2),
                "failing_stage": None,
                "error": None,
            })

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
        # Red pill: for each document, extract features, route it, then:
        #   * BDA    -> call Bedrock Data Automation, write the returned MARKDOWN
        #              to the KB prefix (KB ingests clean text);
        #   * DEFAULT -> write the RAW bytes (Bedrock's own parser ingests it);
        #   * REJECT  -> skip and log.
        # ``force_bda`` (from the CLI) forces the BDA route on every document for
        # testing, regardless of what the router decides.
        prefix = f"runs/{ai.run_id}/kb-docs"

        # --- Pass 1: route every document (NO paid BDA call yet) so the page cap
        # can be enforced before the first Data Automation invocation (spec v2 §5.8).
        # Each entry: (name, key, src_uri, data, route, decision, page_count)
        # src_uri is the ORIGINAL full s3://bucket/key the caller supplied (when it
        # was one), used verbatim for the BDA call — reconstructing it from the
        # bare key dropped the bucket name and made BDA read s3://uploads/... .
        routed: list[tuple[str, str, str, bytes, Route, Any, int]] = []
        for uri in ai.document_uris:
            key = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri
            # Full s3 uri for BDA: keep the caller's uri if it was already one;
            # otherwise build it from the store's bucket (never f"s3://{key}",
            # which drops the bucket). _store_uri falls back to the bare key if the
            # store exposes no bucket (keeps fakes/tests working).
            src_uri = uri if uri.startswith("s3://") else _store_uri(self._clients.store, key)
            name = key.rsplit("/", 1)[-1]
            if not is_stageable_document(name):
                logger.info("skip_non_content_document", name=name)
                continue
            try:
                data = self._clients.store.get_bytes(key)
            except KeyError:
                logger.info("document_not_found", key=key)
                continue
            features = extract_features(name, data)
            decision = route_document(features, self._config)
            route = Route.BDA if self._force_bda else decision.route
            manifest.routing_decisions.append(
                {"file": name, "route": route.value, "rule_id": decision.rule_id,
                 "reason": decision.reason, "forced": self._force_bda}
            )
            logger.info("route_document", file=name, route=route.value, rule_id=decision.rule_id)
            if self._rs and route is not Route.REJECT:
                dest = "Bedrock Data Automation" if route is Route.BDA else "the default parser"
                self._rs.info(
                    f"Routing {name} to {dest} (rule {decision.rule_id}: {decision.reason})",
                    stage="normalize_docs",
                )
            routed.append(
                (name, key, src_uri, data, route, decision, int(features.page_count or 0))
            )

        # Page cap: sum pages of BDA-routed files; fail BEFORE any paid call.
        max_pages = getattr(state, "_max_pages", None)
        if max_pages is not None:
            bda_pages = sum(
                pc for (_n, _k, _u, _d, route, _dec, pc) in routed if route is Route.BDA
            )
            if bda_pages > max_pages:
                if self._rs:
                    self._rs.stage_failed(
                        "normalize_docs",
                        f"Needs ~{bda_pages} scanned pages; limit is {max_pages}.",
                    )
                from kb_agent.exceptions import PageCapExceededError
                raise PageCapExceededError(bda_pages, max_pages)

        # --- Pass 2: now run BDA / default parsing.
        raw_docs: list[tuple[str, bytes, Route]] = []
        md_docs: list[tuple[str, str, Route]] = []
        bda_proc = None
        routing_state: list[dict[str, Any]] = []
        for name, _key, src_uri, data, route, decision, pages in routed:
            routing_state.append({"file": name, "size": len(data), "pages": pages,
                                  "route": "bda" if route is Route.BDA else "default",
                                  "rule_id": decision.rule_id, "reason": decision.reason})
            if route is Route.REJECT:
                logger.info("document_rejected", file=name, reason=decision.reason)
                continue
            if route is Route.BDA:
                if bda_proc is None:
                    bda_proc = BdaProcessor(
                        self._clients.bda, self._clients.store,
                        page_cap=self._config.cost.bda_page_cap_per_run,
                    )
                result = bda_proc.process(name, data, src_uri)
                logger.info("bda_processed", file=name, pages=result.pages_processed,
                            cached=result.cached)
                if self._rs:
                    if result.cached:
                        self._rs.info(f"{name} was parsed before, so the cached result was reused",
                                      stage="normalize_docs")
                    else:
                        self._rs.info(f"Parsed {result.pages_processed} pages from {name}",
                                      stage="normalize_docs")
                md_docs.append((name, result.markdown, route))
            else:  # DEFAULT
                raw_docs.append((name, data, route))

        normalized = []
        if raw_docs:
            normalized += normalize_raw_documents(
                raw_docs, run_id=ai.run_id, store=self._clients.store,
                data_source_prefix=prefix, source_type=SourceType.UPLOADED_DOC,
                owner_id=getattr(state, "_owner_id", None),
            )
        if md_docs:
            normalized += normalize_documents(
                md_docs, run_id=ai.run_id, store=self._clients.store,
                data_source_prefix=prefix, source_type=SourceType.UPLOADED_DOC,
                owner_id=getattr(state, "_owner_id", None),
            )
        state.normalized_docs = normalized
        state.metrics.document_count = len(state.normalized_docs)
        state._routing = routing_state  # type: ignore[attr-defined]
        if self._rs:
            n_bda = sum(1 for r in routing_state if r["route"] == "bda")
            n_def = len(routing_state) - n_bda
            self._rs.info(
                f"Parsed {len(routing_state)} file{'s' if len(routing_state) != 1 else ''}: "
                f"{n_bda} with Bedrock Data Automation, {n_def} with the default parser",
                stage="normalize_docs",
            )

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
        # Write kb_id / data_source_id into _state.json as soon as kb_create ends
        # (spec v2 §5.5) — the outputs screen needs them early.
        if self._rs:
            self._rs.info("Knowledge base created", stage="kb_create")
            self._write_state(state)

    def _stage_kb_ingest(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        if not state.kb_stage:
            return
        if self._rs:
            self._rs.info("Ingestion started", stage="kb_ingest")
        stats = state.kb_stage.ingest()  # may return a dict of stats or a status str
        if self._rs:
            indexed = None
            if isinstance(stats, dict):
                indexed = stats.get("indexed")
            state._ingestion = stats if isinstance(stats, dict) else {}  # type: ignore[attr-defined]
            n = indexed if indexed is not None else state.metrics.document_count
            self._rs.info(f"Ingestion complete: {n} document{'s' if n != 1 else ''} indexed",
                          stage="kb_ingest")
            self._write_state(state)
            self._write_vectors_sample(ai, state)

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

    # --- run-state helpers (only when self._rs is set) -----------------------

    def _write_state(self, state: PipelineState) -> None:
        """Overwrite runs/{run_id}/_state.json at a milestone (spec v2 §5.5)."""
        if not self._rs:
            return
        kb = state.kb_stage
        ingestion = getattr(state, "_ingestion", {}) or {}
        payload = {
            "knowledge_base_id": getattr(kb, "kb_id", None) if kb else None,
            "data_source_id": getattr(kb, "data_source_id", None) if kb else None,
            "vector_bucket": None,
            "index_name": None,
            "embedding_model": getattr(self._clients.embeddings, "model_id", None),
            "chunking": {
                "strategy": self._config.knowledge_base.chunking_strategy,
                "parent_tokens": 1500, "child_tokens": 300, "overlap_tokens": 60,
            },
            "routing": getattr(state, "_routing", []),
            "ingestion": {
                "scanned": ingestion.get("scanned"),
                "indexed": ingestion.get("indexed"),
                "modified": ingestion.get("modified"),
                "failed": ingestion.get("failed"),
                # chunks_written is NOT reliably available from the KB ingestion
                # job (document-level stats only) -> null; the UI hides the chip.
                "chunks_written": None,
            },
            "document_count": state.metrics.document_count,
            "ticket_count": state.metrics.ticket_count,
        }
        self._rs.state(payload)

    def _write_vectors_sample(self, ai: AgentInput, state: PipelineState) -> None:
        """Precompute the run's chunk sample at the end of kb_ingest (spec v2 §5.6).

        Best effort: any failure writes an empty sample and lets the run continue.
        A missing sample must never fail a run. The actual index query is delegated
        to the KB stage (which holds the data-source id and S3 Vectors access); if
        that capability is absent, write an empty sample.
        """
        if not self._rs:
            return
        try:
            sampler = getattr(state.kb_stage, "sample_chunks", None)
            if callable(sampler):
                sample = sampler(run_id=ai.run_id, max_chunks=5)
            else:
                sample = {"chunks": [], "error": "unavailable"}
        except Exception as exc:  # noqa: BLE001 - never fail the run
            logger.warning("vectors_sample_failed", error=str(exc))
            sample = {"chunks": [], "error": "unavailable"}
        self._rs.vectors_sample(sample)


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
