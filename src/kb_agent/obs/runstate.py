"""Run-state writer: the files the backend reads to render live progress (spec v2 §5.3).

The agent is the only thing that holds a run's data AND the S3 access, so it writes
its own progress to ``runs/{run_id}/`` as it goes. The backend Lambdas are thin
readers of these files; they never query Bedrock or the vector index.

Design rules (spec v2 §5.3):
- Every file is written with a SINGLE ``put_text`` (PutObject), so a reader never
  sees a half-written object.
- Every write EXCEPT ``_finished.json`` is best-effort: a failure is swallowed and
  logged, never allowed to fail the pipeline.
- ``_finished.json`` is written in a ``finally`` and retried up to 3 times.
- Event/heartbeat/state bodies carry identifiers, counts and decisions ONLY — never
  document text, ticket text, chunk text or personal data.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

from kb_agent.aws.interfaces import ObjectStore
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)

AGENT_VERSION = "v3"

# Exact reason strings the UI prints (spec v2 §5.3 / §5.4). Do not reword.
SKIP_NO_TICKETS = "No ticket export uploaded"
SKIP_NO_QUESTION_SET = "Runs only when a question set is supplied"

# Stages skipped when no ticket export is supplied (the six ticket stages plus the
# two join stages that need intents). Kept here next to the reason strings; the
# canonical order still comes from pipeline.STAGE_ORDER.
TICKET_DEPENDENT_STAGES = (
    "load_tickets", "adapt_tickets", "clean_tickets",
    "embed_texts", "intent_cluster", "name_clusters",
    "coverage_check", "draft_article",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RunStateWriter:
    """Writes the per-run progress files to the object store under runs/{run_id}/.

    One instance per run. Thread-safe for the event sequence counter and the
    heartbeat timer. All methods are best-effort except :meth:`finished`.
    """

    def __init__(self, store: ObjectStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._prefix = f"runs/{run_id}"
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._current_stage: str | None = None
        self._hb_timer: threading.Timer | None = None
        self._hb_stop = threading.Event()
        self._started_wall = time.monotonic()

    # --- low-level, best-effort put -----------------------------------------
    def _put(self, key: str, obj: dict[str, Any]) -> None:
        try:
            self._store.put_text(f"{self._prefix}/{key}", json.dumps(obj, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 - best effort: never fail the pipeline
            logger.warning("runstate_write_failed", key=key, error=str(exc))

    # --- _started.json -------------------------------------------------------
    def started(
        self,
        *,
        stage_order: list[str],
        skipped: dict[str, str],
        inputs: dict[str, Any],
        owner_id: str | None,
        project_id: str | None = None,
        source_uri: str | None = None,
        vector_store: str | None = None,
    ) -> None:
        self._put(
            "_started.json",
            {
                "run_id": self._run_id,
                "owner_id": owner_id,
                "started_at": _now_iso(),
                "agent_version": AGENT_VERSION,
                "stage_order": stage_order,
                "skipped": skipped,
                "inputs": inputs,
                # Part B additions (additive; absent-friendly for legacy readers).
                "project_id": project_id,
                "source_uri": source_uri,
                "vector_store": vector_store,
            },
        )
        self.event("run_started", stage=None, text="Run created", data={"skipped": list(skipped)})

    def file_outcome(self, name: str, status: str, route: str | None = None) -> None:
        """Emit a per-file outcome event (names + statuses only; B5.2)."""
        self.event("file_outcome", stage="normalize_docs",
                   text=f"{name}: {status}", data={"name": name, "status": status, "route": route})

    # --- _events/{seq}.json --------------------------------------------------
    def event(self, type_: str, *, stage: str | None, text: str, data: dict | None = None) -> None:
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        self._put(
            f"_events/{seq:05d}.json",
            {
                "seq": seq,
                "ts": _now_iso(),
                "t": int(time.monotonic() - self._started_wall),
                "type": type_,
                "stage": stage,
                "text": text,
                "data": data or {},
            },
        )

    def stage_started(self, stage: str, text: str) -> None:
        self._current_stage = stage
        self.event("stage_started", stage=stage, text=text)

    def stage_done(self, stage: str, text: str, data: dict | None = None) -> None:
        self.event("stage_done", stage=stage, text=text, data=data)

    def stage_skipped(self, stage: str, reason: str) -> None:
        self.event("stage_skipped", stage=stage, text=reason)

    def stage_failed(self, stage: str, text: str) -> None:
        self.event("stage_failed", stage=stage, text=text)

    def info(self, text: str, *, stage: str | None = None, data: dict | None = None) -> None:
        self.event("info", stage=stage, text=text, data=data)

    # --- _heartbeat.json (timer thread) -------------------------------------
    def start_heartbeat(self, interval_s: float = 30.0) -> None:
        def beat() -> None:
            if self._hb_stop.is_set():
                return
            self._put("_heartbeat.json", {"ts": _now_iso(), "stage": self._current_stage})
            self._hb_timer = threading.Timer(interval_s, beat)
            self._hb_timer.daemon = True
            self._hb_timer.start()
        # first beat immediately, then on the interval
        self._put("_heartbeat.json", {"ts": _now_iso(), "stage": self._current_stage})
        self._hb_timer = threading.Timer(interval_s, beat)
        self._hb_timer.daemon = True
        self._hb_timer.start()

    def set_stage(self, stage: str | None) -> None:
        self._current_stage = stage

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_timer is not None:
            self._hb_timer.cancel()

    # --- _state.json (overwritten at milestones) ----------------------------
    def state(self, state: dict[str, Any]) -> None:
        self._put("_state.json", state)

    # --- vectors_sample.json -------------------------------------------------
    def vectors_sample(self, sample: dict[str, Any]) -> None:
        self._put("vectors_sample.json", sample)

    # --- _finished.json (NOT best effort; retried) --------------------------
    def finished(self, payload: dict[str, Any]) -> None:
        key = f"{self._prefix}/_finished.json"
        body = json.dumps(payload, separators=(",", ":"))
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._store.put_text(key, body)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        logger.error("runstate_finished_write_failed", run_id=self._run_id, error=str(last_exc))


# The nine dormant stage IDs, in their original order, used only by the legacy
# placeholder path (A4.2) so an unchanged 13-step consumer still sees them.
DORMANT_STAGE_IDS = (
    "load_tickets", "adapt_tickets", "clean_tickets", "embed_texts",
    "intent_cluster", "name_clusters", "kb_retrieve", "coverage_check",
    "draft_article",
)
SKIP_NOT_IN_WORKFLOW = "Not part of this workflow"


def compute_skip_set(has_tickets: bool, has_golden: bool) -> dict[str, str]:
    """The skip set for the ACTIVE KB-only workflow (v3).

    The active ``STAGE_ORDER`` contains only the four KB stages, none of which are
    ever skipped, so this returns an empty set (A4.1). ``has_tickets``/``has_golden``
    are accepted for call-site compatibility and ignored. The legacy 13-stage
    placeholder behaviour lives in :func:`legacy_placeholder_skip_set`.
    """
    return {}


def legacy_placeholder_skip_set() -> dict[str, str]:
    """Skip set when ``workflow.legacy_stage_placeholders`` is true (A4.2).

    Marks every dormant stage id as skipped with the reason
    ``"Not part of this workflow"``, so an unchanged consumer with a fixed
    13-step list shows the nine removed steps as skipped rather than pending
    forever.
    """
    return {s: SKIP_NOT_IN_WORKFLOW for s in DORMANT_STAGE_IDS}
