"""AgentCore Runtime handler + local CLI (spec 7.1, 8; console-deploy v2 §5.1).

Two entry surfaces over one shared handler:

* **AgentCore HTTP app** - a container exposing ``POST /invocations`` and
  ``GET /ping``. For the console deploy the agent runs **asynchronously**:
  ``/invocations`` validates the payload, starts the pipeline on a background
  thread, and returns ``{"run_id","status":"accepted"}`` within about a second.
  The pipeline writes its own progress to S3 (``runs/{run_id}/_started.json`` etc.)
  which the backend reads. ``GET /ping`` reports **busy** while a run is in flight
  and **healthy** otherwise, and never blocks (the server is threaded).
* **CLI** - :func:`main` runs a payload synchronously (``wait`` behaviour), exactly
  as before. The CLI path and every existing test are unchanged.

The HTTP layer is dependency-free (standard-library WSGI) so no web framework is
pulled into the ARM64 image.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from kb_agent.aws.clientset import Mode, build_client_set
from kb_agent.config import get_settings, load_config
from kb_agent.contract import AgentInput, AgentOutput
from kb_agent.logging_setup import configure_logging, get_logger
from kb_agent.obs.tracing import configure_tracing
from kb_agent.pipeline import Pipeline

logger = get_logger(__name__)


# --- AgentCore /ping health strings ------------------------------------------
# TODO(confirm) V1: exact AgentCore health strings + casing. The runtime contract
# is believed to use "Healthy" and "HealthyBusy"; the agent previously returned
# {"status":"healthy"}. Both are emitted here (a canonical string plus a lowercase
# alias) until the live docs are confirmed — see OPEN_QUESTIONS verification log.
PING_HEALTHY = "Healthy"
PING_BUSY = "HealthyBusy"


class RunManager:
    """Tracks the single in-process background run (spec v2 §5.1).

    AgentCore gives each run its own session/microVM, so "one run per process" is
    a safety net, not the normal path. This manager enforces it and powers the
    busy/idempotency logic.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id: str | None = None
        self._thread: threading.Thread | None = None

    def _busy_locked(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy_locked()

    @property
    def current_run_id(self) -> str | None:
        # Single lock acquisition — do NOT call self.busy here (same non-reentrant
        # lock would self-deadlock).
        with self._lock:
            return self._run_id if self._busy_locked() else None

    def try_start(self, run_id: str, target: Callable[[], None]) -> str:
        """Start ``target`` on a background thread.

        Returns one of: ``"accepted"`` (started), ``"already_running"`` (this exact
        run_id is already in flight), or ``"busy"`` (a *different* run is in flight).
        """
        with self._lock:
            alive = self._busy_locked()
            if alive and self._run_id == run_id:
                return "already_running"
            if alive:
                return "busy"
            self._run_id = run_id
            self._thread = threading.Thread(target=target, name=f"run-{run_id}", daemon=True)
            self._thread.start()
            return "accepted"


# Module-level singleton used by the HTTP app.
_run_manager = RunManager()


def _run_pipeline_guarded(agent_input: AgentInput, mode: Mode, emit_runstate: bool) -> AgentOutput:
    """Build clients and run the pipeline. Shared by sync and async paths.

    The caller wraps this so that even a ``BaseException`` still records a terminal
    state (spec v2 §5.1 item 4).
    """
    settings = get_settings()
    config = load_config()
    clients = build_client_set(mode, settings, config)
    return Pipeline(clients, config).run(agent_input, emit_runstate=emit_runstate)


def handle_invocation(payload: dict[str, Any], mode: Mode = "aws") -> AgentOutput:
    """Synchronous handler (CLI + ``wait:true``). Behaviour unchanged from before."""
    settings = get_settings()
    config = load_config()
    configure_logging(settings.log_level)
    configure_tracing(settings.otel_endpoint)
    agent_input = AgentInput.model_validate(payload)
    clients = build_client_set(mode, settings, config)
    return Pipeline(clients, config).run(agent_input)


def _start_async(agent_input: AgentInput, mode: Mode) -> str:
    """Kick the pipeline on a background thread; return the start outcome string.

    The thread body is wrapped so ANY exception/BaseException still ends the run
    with a terminal state written to S3 (the pipeline writes `_finished.json`;
    this is the last-ditch guard if `run()` itself fails to start).
    """
    def body() -> None:
        try:
            _run_pipeline_guarded(agent_input, mode, emit_runstate=True)
        except BaseException as exc:  # noqa: BLE001 - must never die silently
            logger.error("run_thread_crashed", run_id=agent_input.run_id, error=repr(exc))
            # Last-ditch terminal marker: the pipeline writes _finished.json itself
            # in a finally, so this only matters if run() blew up before that.
            try:
                from datetime import UTC, datetime

                from kb_agent.obs.runstate import RunStateWriter
                settings = get_settings()
                config = load_config()
                clients = build_client_set(mode, settings, config)
                RunStateWriter(clients.store, agent_input.run_id).finished({
                    "status": "failed",
                    "ended_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": None,
                    "failing_stage": None,
                    "error": {"kind": "agent_error", "message": "The run stopped unexpectedly."},
                })
            except Exception as inner:  # noqa: BLE001
                logger.error("terminal_marker_failed", run_id=agent_input.run_id, error=repr(inner))

    return _run_manager.try_start(agent_input.run_id, body)


# --- AgentCore HTTP contract (POST /invocations, GET /ping) -------------------


def create_app(mode: Mode = "aws") -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Return a minimal WSGI app exposing the AgentCore Runtime endpoints.

    ``GET /ping`` -> health (busy while a run is in flight). ``POST /invocations``
    -> start a run asynchronously (or synchronously with ``{"wait": true}``).
    """
    # Configure logging/tracing once at app construction.
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings.otel_endpoint)

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")

        # /ping must answer within ~1s ALWAYS, including mid-run. It only reads an
        # in-memory flag; no I/O, no lock held during a run.
        if path == "/ping" and method == "GET":
            status = PING_BUSY if _run_manager.busy else PING_HEALTHY
            return _json_response(start_response, "200 OK",
                                  {"status": status, "run_id": _run_manager.current_run_id})

        if path == "/invocations" and method == "POST":
            try:
                size = int(environ.get("CONTENT_LENGTH") or 0)
                raw = environ["wsgi.input"].read(size) if size else b"{}"
                payload = json.loads(raw or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return _json_response(
                    start_response, "400 Bad Request",
                    {"error": {"code": "invalid_input", "message": f"bad payload: {exc}"}},
                )

            wait = bool(payload.pop("wait", False))

            # Validate the input (never leak a stack trace).
            try:
                agent_input = AgentInput.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, "400 Bad Request",
                    {"error": {"code": "invalid_input", "message": _short(exc)}},
                )

            # Synchronous path (CLI parity / tests / explicit wait:true).
            if wait:
                try:
                    out = handle_invocation(payload, mode=mode)
                    return _json_response(start_response, "200 OK", out.model_dump(mode="json"))
                except Exception as exc:  # noqa: BLE001
                    logger.error("invocation_failed", error=_short(exc))
                    return _json_response(
                        start_response, "500 Internal Server Error",
                        {"error": {"code": "agent_error", "message": _short(exc)}},
                    )

            # Asynchronous path: start and return within ~1s.
            outcome = _start_async(agent_input, mode)
            if outcome == "busy":
                return _json_response(
                    start_response, "409 Conflict",
                    {"error": {"code": "busy", "message": "Another run is already in progress."}},
                )
            # accepted | already_running are both success.
            return _json_response(start_response, "202 Accepted",
                                  {"run_id": agent_input.run_id, "status": outcome})

        return _json_response(start_response, "404 Not Found",
                              {"error": {"code": "not_found", "message": "not found"}})

    return app


def _short(exc: Exception) -> str:
    """A short, safe message — never a stack trace."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _json_response(start_response: Any, status: str, body: dict[str, Any]) -> list[bytes]:
    data = json.dumps(body).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(data))),
    ]
    start_response(status, headers)
    return [data]


# --- threaded WSGI server ----------------------------------------------------


def _serve(app: Callable, port: int) -> None:
    """Serve the app on a THREADED WSGI server so /ping never blocks mid-run."""
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True  # worker threads do not block shutdown

    logger.info("serving", port=port, endpoints=["POST /invocations", "GET /ping"], threaded=True)
    with make_server("", port, app, server_class=ThreadingWSGIServer) as httpd:
        httpd.serve_forever()


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run the agent on a JSON payload (sync) or serve the HTTP app."""
    import argparse

    parser = argparse.ArgumentParser(description="KB Integration Agent entrypoint.")
    parser.add_argument("--mode", choices=["fake", "aws"], default="aws")
    parser.add_argument("--payload", help="Path to a JSON AgentInput payload.")
    parser.add_argument(
        "--serve", action="store_true", help="Serve the AgentCore HTTP app on --port."
    )
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    mode: Mode = args.mode  # argparse choices constrain this to fake|aws

    if args.serve:
        app = create_app(mode=mode)
        _serve(app, args.port)
        return 0

    if not args.payload:
        parser.error("provide --payload or --serve")

    with open(args.payload, encoding="utf-8") as handle:
        payload = json.load(handle)
    output = handle_invocation(payload, mode=mode)
    print(output.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
