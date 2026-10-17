"""AgentCore Runtime handler + local CLI (spec 7.1, 8; Part A item 4).

Two entry surfaces, no business logic (spec 7.4: the entrypoint parses input,
calls the pipeline, formats output):

* **AgentCore HTTP app** - the verified Runtime contract requires a container
  exposing ``POST /invocations`` and ``GET /ping`` (Part A item 4). :func:`create_app`
  builds a minimal WSGI app providing exactly those, so the same image runs under
  AgentCore and can be contract-tested locally with a stub orchestrator.
* **CLI** - :func:`main` lets a developer invoke the agent from the shell.

The HTTP layer is dependency-free (standard-library WSGI) so no web framework is
pulled into the ARM64 image unnecessarily.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kb_agent.aws.clientset import Mode, build_client_set
from kb_agent.config import get_settings, load_config
from kb_agent.contract import AgentInput, AgentOutput
from kb_agent.logging_setup import configure_logging, get_logger
from kb_agent.obs.tracing import configure_tracing
from kb_agent.pipeline import Pipeline

logger = get_logger(__name__)


def handle_invocation(payload: dict[str, Any], mode: Mode = "aws") -> AgentOutput:
    """Core handler: validate input, run the pipeline, return the output.

    Shared by both the HTTP app and the CLI. ``mode`` selects the client set;
    AgentCore deployments use ``aws``.
    """
    settings = get_settings()
    config = load_config()
    configure_logging(settings.log_level)
    configure_tracing(settings.otel_endpoint)

    agent_input = AgentInput.model_validate(payload)
    clients = build_client_set(mode, settings, config)
    return Pipeline(clients, config).run(agent_input)


# --- AgentCore HTTP contract (POST /invocations, GET /ping) -------------------


def create_app(mode: Mode = "aws") -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Return a minimal WSGI app exposing the AgentCore Runtime endpoints.

    Contract (Part A item 4): ``GET /ping`` -> health, ``POST /invocations`` ->
    run. Returns JSON. No web framework, so the container stays small.
    """

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")

        if path == "/ping" and method == "GET":
            return _json_response(start_response, "200 OK", {"status": "healthy"})

        if path == "/invocations" and method == "POST":
            try:
                size = int(environ.get("CONTENT_LENGTH") or 0)
                body = environ["wsgi.input"].read(size) if size else b"{}"
                payload = json.loads(body or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return _json_response(
                    start_response, "400 Bad Request", {"error": f"bad payload: {exc}"}
                )
            try:
                output = handle_invocation(payload, mode=mode)
            except Exception as exc:  # noqa: BLE001 - return a clean 500, do not crash the server
                logger.error("invocation_failed", error=str(exc))
                return _json_response(
                    start_response, "500 Internal Server Error", {"error": str(exc)}
                )
            return _json_response(start_response, "200 OK", output.model_dump(mode="json"))

        return _json_response(start_response, "404 Not Found", {"error": "not found"})

    return app


def _json_response(start_response: Any, status: str, body: dict[str, Any]) -> list[bytes]:
    data = json.dumps(body).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(data))),
    ]
    start_response(status, headers)
    return [data]


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run the agent on a JSON payload or serve the HTTP app."""
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
        from wsgiref.simple_server import make_server

        app = create_app(mode=mode)
        logger.info("serving", port=args.port, endpoints=["POST /invocations", "GET /ping"])
        with make_server("", args.port, app) as httpd:
            httpd.serve_forever()
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
