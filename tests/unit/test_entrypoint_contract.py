"""Contract test for the AgentCore HTTP app (spec 7.6, Part A item 4).

Exercises the exact Runtime contract - GET /ping and POST /invocations - using a
WSGI test client against the fake-mode app, so it runs offline. This is the
"stub orchestrator" posting the payload the real orchestrator will send and
asserting the response shape.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from kb_agent.entrypoint import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def _call(app, method: str, path: str, body: bytes = b"") -> tuple[str, dict]:
    captured: dict = {}

    def start_response(status: str, headers: list) -> None:
        captured["status"] = status

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    chunks = app(environ, start_response)
    payload = json.loads(b"".join(chunks) or b"{}")
    return captured["status"], payload


def test_ping_returns_healthy() -> None:
    app = create_app(mode="fake")
    status, body = _call(app, "GET", "/ping")
    assert status.startswith("200")
    assert body["status"] == "healthy"


def test_invocations_runs_pipeline_and_returns_contract_shape() -> None:
    app = create_app(mode="fake")

    # Minimal payload: no tickets/docs. The pipeline still completes and returns
    # the AgentOutput contract shape (spec 8).
    payload = json.dumps({"run_id": "contract-1"}).encode()
    status, body = _call(app, "POST", "/invocations", payload)

    assert status.startswith("200")
    # AgentOutput contract fields (spec 8).
    for key in ("run_id", "status", "stage", "artifacts", "metrics"):
        assert key in body, f"missing contract field: {key}"
    assert body["run_id"] == "contract-1"
    assert body["status"] in {"completed", "running", "needs_review", "failed"}


def test_bad_payload_returns_400() -> None:
    app = create_app(mode="fake")
    status, body = _call(app, "POST", "/invocations", b"{not json}")
    assert status.startswith("400")
    assert "error" in body


def test_unknown_path_returns_404() -> None:
    app = create_app(mode="fake")
    status, _ = _call(app, "GET", "/nope")
    assert status.startswith("404")
