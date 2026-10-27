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
from typing import Any, Literal

from kb_agent.aws.clientset import ClientSet
from kb_agent.config import AppConfig, get_settings
from kb_agent.contract import AgentInput, AgentOutput, RunMetrics
from kb_agent.documents.bda import BdaProcessor
from kb_agent.documents.knowledge_base import KnowledgeBaseStage
from kb_agent.documents.multifile import MultiFileNormalizer, discover
from kb_agent.documents.normalize import NormalizedDocument
from kb_agent.documents.outcomes import NormalizationResult
from kb_agent.join.report import build_report, render_markdown
from kb_agent.logging_setup import bind_run_id, get_logger
from kb_agent.obs.audit import RunManifest, library_versions
from kb_agent.obs.runstate import RunStateWriter, compute_skip_set
from kb_agent.obs.timing import StageTimer
from kb_agent.obs.tracing import stage_span

# --- DORMANT (v3 KB-only reorg): the ticket/join/review stages and their helpers
# moved to kb_agent.dormant.*; they are NOT wired into the active workflow. The
# nine dormant _stage_* methods live on DormantStagesMixin (dormant/pipeline_stages.py),
# which the active Pipeline does NOT inherit. To re-enable them: inherit the mixin
# and restore LEGACY_STAGE_ORDER_13 as the active order.
# DORMANT: from kb_agent.dormant.join.coverage import coverage_check
# DORMANT: from kb_agent.dormant.join.drafting import draft_articles
# DORMANT: from kb_agent.dormant.review.state import ReviewRegistry
# DORMANT: from kb_agent.dormant.tickets.adapters.sample import SampleAdapter
# DORMANT: from kb_agent.dormant.tickets.clean import clean_tickets, embedding_texts
# DORMANT: from kb_agent.dormant.tickets.cluster import (
# DORMANT:     cluster_embeddings, should_use_density_clustering)
# DORMANT: from kb_agent.dormant.tickets.embed import EmbeddingCache
# DORMANT: from kb_agent.dormant.tickets.loaders import load_tickets_uri
# DORMANT: from kb_agent.dormant.tickets.naming import name_clusters
# DORMANT: from kb_agent.dormant.documents_extras import recall_at_5_from_golden

logger = get_logger(__name__)

# The active KB-only happy-path stage order (v3). Order is CODE (spec 6).
KB_ONLY_STAGE_ORDER = [
    "normalize_docs",
    "kb_create",
    "kb_ingest",
    "write_report",
]

# The original 13-stage order, kept for the legacy_stage_placeholders flag (A4.2)
# so an unchanged consumer with a fixed 13-step list still sees them (as skipped).
LEGACY_STAGE_ORDER_13 = [
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

# Back-compat alias: existing code/tests reference STAGE_ORDER. In the KB-only
# workflow it IS the four-stage order.
STAGE_ORDER = KB_ONLY_STAGE_ORDER


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
        ParsingError,
        ValidationError,
    )
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

        # Resolve the project identity (Part B). project_id is the stable key the
        # vector index / KB / data source / source prefix / lock all hang off. When
        # provisioning is enabled, documents are staged under the PROJECT prefix
        # (accumulates across runs); otherwise they stay under the per-run prefix
        # so the Part A behaviour is unchanged.
        from kb_agent import project as _project
        pid = agent_input.project_id
        project_id = _project.validate_project_id(pid) if pid else _project.derive_project_id(
            agent_input.run_id
        )
        state._project_id = project_id  # type: ignore[attr-defined]
        if self._config.provisioning.enabled:
            state._kb_prefix = _project.source_prefix(  # type: ignore[attr-defined]
                project_id, root=self._config.provisioning.kb_source_root
            ).rstrip("/")
        else:
            state._kb_prefix = f"runs/{agent_input.run_id}/kb-docs"  # type: ignore[attr-defined]
        state._lock = None  # type: ignore[attr-defined]

        # run-state writer (opt-in). KB-only: the active skip set is empty. When
        # the legacy placeholder flag is on (A4.2), REPORT the original 13-stage
        # order with the nine dormant stages marked skipped — this only affects
        # what is reported in _started.json / manifest, never execution (the loop
        # below always iterates the four active stages).
        rs: RunStateWriter | None = None
        legacy = self._config.workflow.legacy_stage_placeholders
        if legacy:
            from kb_agent.obs.runstate import legacy_placeholder_skip_set
            reported_stage_order = list(LEGACY_STAGE_ORDER_13)
            skipped = legacy_placeholder_skip_set()
        else:
            reported_stage_order = list(STAGE_ORDER)
            skipped = compute_skip_set(
                has_tickets=bool(agent_input.ticket_uri),
                has_golden=bool(golden),
            )
        if emit_runstate:
            rs = RunStateWriter(self._clients.store, agent_input.run_id)
            rs.started(
                stage_order=reported_stage_order,
                skipped=skipped,
                inputs={
                    "documents": [{"uri": u} for u in agent_input.document_uris],
                    "source_uri": agent_input.source_uri,
                    "ticket": agent_input.ticket_uri,
                },
                owner_id=agent_input.owner_id,
                project_id=getattr(state, "_project_id", None),
                source_uri=agent_input.source_uri,
                vector_store=agent_input.vector_store or self._config.vector_store.type,
            )
            for stg, reason in skipped.items():
                rs.stage_skipped(stg, reason)
            # Deprecated input: a ticket export is accepted and ignored in the
            # KB-only workflow (A4.5). Emit exactly one info event when present.
            if agent_input.ticket_uri:
                rs.info(
                    "A ticket export was supplied and ignored: ticket analysis "
                    "is not part of this workflow."
                )
            rs.start_heartbeat()
        # Record the dormant placeholders in the manifest too when legacy is on.
        if legacy:
            for stg, reason in skipped.items():
                manifest.record_stage(stg, "skipped", output=reason)
        self._rs = rs

        start_index = 0
        if agent_input.resume_from:
            if agent_input.resume_from not in STAGE_ORDER:
                # A resume_from naming a dormant/unknown stage is invalid input
                # (A4.1). The HTTP layer maps ValueError -> 400 invalid_input; we
                # list the valid KB-only stages so the caller can correct it.
                raise ValueError(
                    f"invalid resume_from {agent_input.resume_from!r}; "
                    f"valid stages are {STAGE_ORDER}"
                )
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
            # Release the per-project lock if one was acquired (B4.4), no matter
            # how the run ended.
            lock = getattr(state, "_lock", None)
            if lock is not None:
                try:
                    lock.release()
                except Exception:  # noqa: BLE001 - release must never mask a result
                    logger.warning("project_lock_release_failed")

        state.metrics.stage_durations_sec = timer.durations
        artifacts = self._finalise(agent_input.run_id, state, manifest)
        manifest.metrics = state.metrics.model_dump(mode="json")
        manifest.finish()
        self._write_manifest(agent_input.run_id, manifest)

        # Run disposition from the per-file outcomes (B3.5): if EVERY file ended
        # rejected/failed/ignored the run FAILS with no_documents_ingested; a
        # partial failure is still completed, with warnings.
        from kb_agent.documents.outcomes import aggregate_run_status
        outcomes = getattr(state, "_file_outcomes", []) or []
        disposition, file_warnings = aggregate_run_status(outcomes)
        warnings = list(getattr(state, "_warnings", [])) + file_warnings
        binding_uri = getattr(state, "_binding_uri", None)
        out_project_id = getattr(state, "_project_id", None)
        kb_id = state.kb_stage.kb_id if state.kb_stage else None

        if outcomes and disposition == "no_documents_ingested":
            # Nothing usable was produced; surface a clear terminal failure.
            if rs:
                rs.stop_heartbeat()
                rs.finished({
                    "status": "failed", "ended_at": _utcnow_iso(),
                    "elapsed_seconds": round(sum(timer.durations.values()), 2),
                    "failing_stage": "normalize_docs",
                    "error": {"kind": "no_documents_ingested",
                              "message": "No supported documents could be ingested."},
                })
            return AgentOutput(
                run_id=agent_input.run_id, status="failed", stage="normalize_docs",
                artifacts=artifacts, metrics=state.metrics,
                project_id=out_project_id, knowledge_base_id=kb_id,
                knowledge_binding_uri=binding_uri, warnings=warnings,
                errors=[{"stage": "normalize_docs", "kind": "no_documents_ingested",
                         "message": "No supported documents could be ingested."}],  # type: ignore[list-item]
            )

        # Terminal status: completed (no human-review gate). The legacy flag
        # re-emits it as needs_review for an unchanged 13-step consumer (B1.3).
        final_status: Literal["completed", "needs_review"] = (
            "needs_review"
            if self._config.workflow.legacy_status_needs_review
            else "completed"
        )
        if rs:
            rs.stop_heartbeat()
            rs.finished({
                "status": final_status,
                "ended_at": _utcnow_iso(),
                "elapsed_seconds": round(sum(timer.durations.values()), 2),
                "failing_stage": None,
                "error": None,
            })

        return AgentOutput(
            run_id=agent_input.run_id,
            status=final_status,
            stage="write_report",
            artifacts=artifacts,
            metrics=state.metrics,
            project_id=out_project_id,
            knowledge_base_id=kb_id,
            knowledge_binding_uri=binding_uri,
            warnings=warnings,
            # DORMANT: review registry is unwired in the KB-only workflow (A4).
            pending_review=[],
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

    # --- DORMANT stages (moved to kb_agent.dormant.pipeline_stages.DormantStagesMixin):
    # _stage_load_tickets, _stage_adapt_tickets, _stage_clean_tickets,
    # _stage_embed_texts, _stage_intent_cluster, _stage_name_clusters (blue pill);
    # _stage_kb_retrieve; _stage_coverage_check, _stage_draft_article (join).
    # They are not in KB_ONLY_STAGE_ORDER, so they are never dispatched here.

    # --- red pill stages -----------------------------------------------------

    def _stage_normalize_docs(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        # Multi-file normalisation (Part B B3): discover every source object,
        # route each on cheap signals, dedupe by content hash, then process in a
        # bounded pool - BDA (markdown), CONVERT (structured -> markdown) or
        # DEFAULT (raw bytes). One failed file never fails the run; each file gets
        # a FileOutcome. Markdown/converted extractions also get a per-run copy
        # under runs/{run_id}/extractions/ for review (B4.3). The destination
        # prefix is the project prefix when provisioning is enabled (accumulates
        # across runs), else the per-run prefix (Part A behaviour).
        prefix = getattr(state, "_kb_prefix", f"runs/{ai.run_id}/kb-docs")

        def _emit(msg: str) -> None:
            if self._rs:
                self._rs.info(msg, stage="normalize_docs")

        discovered, ignored = discover(
            self._clients.store, ai, max_files=int(self._config.source.max_files)
        )
        bda_proc = BdaProcessor(self._clients.bda, self._clients.store)
        normalizer = MultiFileNormalizer(
            self._clients.store, bda_proc, self._config,
            run_id=ai.run_id, kb_prefix=prefix,
            owner_id=getattr(state, "_owner_id", None),
            force_bda=self._force_bda, emit=_emit,
        )
        result = normalizer.run(discovered)
        all_outcomes = ignored + result.outcomes
        all_outcomes.sort(key=lambda o: o.name)

        # Record routing decisions in the manifest (names + routes only).
        for o in all_outcomes:
            manifest.routing_decisions.append(
                {"file": o.name, "route": o.route or "ignored", "rule_id": o.rule_id,
                 "reason": o.status_reason or o.reason, "forced": self._force_bda}
            )
            logger.info("file_outcome", file=o.name, status=o.status, route=o.route)

        # Staged files become the KB documents; expose for kb_create/ingest.
        staged = [o for o in all_outcomes if o.status == "staged"]
        state.normalized_docs = [
            NormalizedDocument(o.kb_key or "", f"{o.kb_key}.metadata.json", (o.content_hash or ""))
            for o in staged
        ]
        state.metrics.document_count = len(staged)
        state._file_outcomes = all_outcomes  # type: ignore[attr-defined]
        state._pages_processed = bda_proc.pages_processed  # type: ignore[attr-defined]
        state._chunking_strategy = ai.chunking_strategy or (  # type: ignore[attr-defined]
            self._config.knowledge_base.chunking_strategy
        )
        # Multi-file metrics (B5.3). files_total excludes ignored noise.
        counts = NormalizationResult(outcomes=all_outcomes).counts()
        state.metrics.files_total = sum(
            counts.get(s, 0) for s in
            ("staged", "ingested", "rejected", "failed", "skipped_duplicate", "unchanged")
        )
        state.metrics.files_ingested = len(staged) + counts.get("unchanged", 0)
        state.metrics.files_rejected = counts.get("rejected", 0)
        state.metrics.files_failed = counts.get("failed", 0)
        state.metrics.pages_processed = bda_proc.pages_processed
        state.metrics.bda_cache_hits = sum(1 for o in all_outcomes if o.bda_cached)
        # Back-compat routing view for _state.json (names/routes/sizes only).
        state._routing = [  # type: ignore[attr-defined]
            {"file": o.name, "size": o.size, "pages": o.pages,
             "route": o.route or "ignored", "rule_id": o.rule_id, "reason": o.reason}
            for o in all_outcomes
        ]

        if self._rs:
            for o in all_outcomes:
                self._rs.file_outcome(o.name, o.status, o.route)
            counts = NormalizationResult(outcomes=all_outcomes).counts()
            n_bda = sum(1 for o in all_outcomes if o.route == "bda")
            n_conv = sum(1 for o in all_outcomes if o.route == "convert")
            n_def = sum(1 for o in all_outcomes if o.route == "default")
            self._rs.info(
                f"Processed {len(all_outcomes)} file{'s' if len(all_outcomes) != 1 else ''}: "
                f"{n_def} default parser, {n_bda} Bedrock Data Automation, "
                f"{n_conv} converted; {counts.get('rejected', 0)} rejected, "
                f"{counts.get('ignored', 0)} ignored.",
                stage="normalize_docs",
            )

    def _stage_kb_create(
        self,
        ai: AgentInput,
        state: PipelineState,
        manifest: RunManifest,
        golden: list[dict] | None,
    ) -> None:
        project_id = getattr(state, "_project_id", None) or ai.run_id
        chunking = ai.chunking_strategy or self._config.knowledge_base.chunking_strategy

        if self._config.provisioning.enabled:
            # Acquire the per-project lock FIRST (held through kb_ingest; released
            # in run()'s finally). Two runs on one project must not provision or
            # ingest at once (B4.4).
            rs = self._rs
            emit = (lambda m: rs.info(m, stage="kb_create")) if rs else None

            def _emit_event(type_: str, data: dict) -> None:
                if rs:
                    rs.event(
                        type_, stage="kb_create",
                        text=f"{data.get('resource_type')} {data.get('name')}", data=data,
                    )

            from kb_agent.lock import ProjectLock
            from kb_agent.project import lock_key
            lock = ProjectLock(
                self._clients.store,
                lock_key(project_id, root=self._config.provisioning.kb_source_root),
                ai.run_id,
                ttl_seconds=self._config.provisioning.lock_ttl_seconds,
                wait_seconds=self._config.provisioning.lock_wait_seconds,
            )
            lock.acquire(emit=emit)
            state._lock = lock  # type: ignore[attr-defined]

            from kb_agent.provisioning import Provisioner
            provisioner = Provisioner(
                self._clients.store, self._clients.vector_admin,
                self._clients.knowledge_base, self._config, get_settings(),
                emit=emit, emit_event=_emit_event,
            )
            result = provisioner.provision(project_id, ai.run_id, chunking_strategy=chunking)
            # Bind a KB stage to the provisioned ids (no second create).
            stage = KnowledgeBaseStage(self._clients.knowledge_base, self._config)
            stage._kb_id = result.knowledge_base_id  # type: ignore[attr-defined]
            state.kb_stage = stage
            state._provisioning = result  # type: ignore[attr-defined]
        else:
            # Fallback (Part A behaviour): per-run KB, no provisioning/lock.
            kb_prefix = getattr(state, "_kb_prefix", f"runs/{ai.run_id}/kb-docs")
            stage = KnowledgeBaseStage(self._clients.knowledge_base, self._config)
            stage.create(f"kb-{ai.run_id}", kb_prefix)
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
        ingestion = stats if isinstance(stats, dict) else {}
        state._ingestion = ingestion  # type: ignore[attr-defined]

        # Upgrade each staged file to ingested. The ingestion API reports
        # aggregate counts + a failureReasons LIST (no per-document-key map; see
        # docs/phase2-design.md §0.3), so we mark staged files ingested and raise
        # a run-level warning when the job reports failures, rather than guessing
        # which file failed.
        for o in getattr(state, "_file_outcomes", []) or []:
            if o.status == "staged":
                o.status = "ingested"
        n_failed = int(ingestion.get("failed") or 0)
        if n_failed:
            warns = getattr(state, "_warnings", [])  # type: ignore[attr-defined]
            warns.append(f"ingestion reported {n_failed} document(s) failed to index")
            state._warnings = warns  # type: ignore[attr-defined]

        # Write the handover binding now that ids + ingestion stats exist (B5.3).
        self._write_binding(ai, state, ingestion)

        if self._rs:
            indexed = ingestion.get("indexed")
            n = indexed if indexed is not None else state.metrics.document_count
            self._rs.info(f"Ingestion complete: {n} document{'s' if n != 1 else ''} indexed",
                          stage="kb_ingest")
            self._write_state(state)
            self._write_vectors_sample(ai, state)

    # --- DORMANT: _stage_kb_retrieve, _stage_coverage_check, _stage_draft_article
    # moved to kb_agent.dormant.pipeline_stages.DormantStagesMixin (not dispatched).

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
        """Write the report (MD+JSON). KB-only: no review registry (A4).

        DORMANT: the ReviewRegistry / article-review wiring moved out with the
        join stages; the KB-only workflow has no approval step, so ``manifest``
        carries no review_records and no draft artefacts are registered.
        """
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
        prov = getattr(state, "_provisioning", None)
        outcomes = getattr(state, "_file_outcomes", []) or []
        vector_store_block: dict[str, Any] | None = None
        provisioning_steps: list[dict] = []
        if prov is not None:
            vector_store_block = {
                "type": self._config.vector_store.type,
                "bucket": prov.vector_bucket,
                "index_name": prov.index_name,
                "index_arn": prov.index_arn,
                "dimension": prov.dimension,
                "distance_metric": prov.distance_metric,
            }
            provisioning_steps = prov.steps
        payload = {
            "project_id": getattr(state, "_project_id", None),
            "knowledge_base_id": getattr(kb, "kb_id", None) if kb else None,
            "data_source_id": getattr(kb, "data_source_id", None) if kb else None,
            "vector_store": vector_store_block,
            # Back-compat flat fields some readers still use.
            "vector_bucket": prov.vector_bucket if prov else None,
            "index_name": prov.index_name if prov else None,
            "embedding_model": getattr(self._clients.embeddings, "model_id", None),
            "chunking": {
                "strategy": (
                    getattr(state, "_chunking_strategy", None)
                    or self._config.knowledge_base.chunking_strategy
                ),
                "parent_tokens": 1500, "child_tokens": 300, "overlap_tokens": 60,
            },
            "provisioning": provisioning_steps,
            "routing": getattr(state, "_routing", []),
            # Per-file outcomes (names/statuses/routes only; B3.5 / B5.2).
            "files": [o.model_dump() for o in outcomes],
            "ingestion": {
                "job_id": ingestion.get("job_id"),
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

    def _write_binding(self, ai: AgentInput, state: PipelineState, ingestion: dict) -> None:
        """Build + write knowledge_binding.json (run + project copies; B5.3).

        Best-effort: a binding-write failure is logged but never fails the run.
        The run uri is stashed on state for AgentOutput.knowledge_binding_uri.
        """
        from kb_agent.binding import build_binding, write_binding

        kb = state.kb_stage
        prov = getattr(state, "_provisioning", None)
        project_id = getattr(state, "_project_id", None) or ai.run_id
        settings = get_settings()
        chunking_strategy = getattr(state, "_chunking_strategy", None) or (
            self._config.knowledge_base.chunking_strategy
        )
        if prov is not None:
            vector_store = {
                "type": self._config.vector_store.type,
                "bucket": prov.vector_bucket, "index_name": prov.index_name,
                "index_arn": prov.index_arn, "dimension": prov.dimension,
                "distance_metric": prov.distance_metric,
            }
            kb_block = {
                "id": prov.knowledge_base_id, "arn": None,
                "name": prov.index_name, "data_source_id": prov.data_source_id,
                "status": "ACTIVE",
            }
        else:
            v = self._config.vector_store
            vector_store = {
                "type": v.type, "bucket": self._config.provisioning.vector_bucket_name,
                "index_name": None, "index_arn": settings.vector_index_arn or None,
                "dimension": v.dimension, "distance_metric": v.distance_metric,
            }
            kb_block = {
                "id": getattr(kb, "kb_id", None) if kb else None, "arn": None,
                "name": None, "data_source_id": getattr(kb, "data_source_id", None) if kb else None,
                "status": "ACTIVE",
            }
        try:
            binding = build_binding(
                project_id=project_id, run_id=ai.run_id, created_at=_utcnow_iso(),
                region=ai.region or settings.aws_region, account_id=None,
                status="ready",
                knowledge_base=kb_block, vector_store=vector_store,
                embedding_model_id=settings.embedding_model_id,
                chunking={
                    "strategy": chunking_strategy,
                    "parent_tokens": 1500, "child_tokens": 300, "overlap_tokens": 60,
                },
                source={
                    "bucket": settings.s3_bucket,
                    "kb_prefix": getattr(state, "_kb_prefix", f"runs/{ai.run_id}/kb-docs") + "/",
                },
                ingestion={
                    "job_id": ingestion.get("job_id"),
                    "scanned": ingestion.get("scanned"),
                    "indexed": ingestion.get("indexed"),
                    "failed": ingestion.get("failed"),
                },
                outcomes=getattr(state, "_file_outcomes", []) or [],
            )
            uri = write_binding(self._clients.store, binding)
            state._binding_uri = uri  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - binding is best-effort
            logger.warning("binding_write_failed", error=str(exc))

# DORMANT: _silhouette_excluding_noise moved to
# kb_agent.dormant.pipeline_stages (it is used only by the dormant
# _stage_intent_cluster, and was the sole reason this module imported sklearn).
