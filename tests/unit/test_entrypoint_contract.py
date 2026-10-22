"""Contract test for the AgentCore HTTP app (console-deploy v2 §5.1, §5.10).

Exercises the Runtime contract - GET /ping and POST /invocations - using a WSGI
test client against the fake-mode app, so it runs offline. The v2 contract is
ASYNC: /invocations returns {"status":"accepted"} within ~1s and the pipeline runs
on a background thread; a synchronous run is still available via {"wait": true}.
"""

from __future__ import annotations

import io
import json
import time

from kb_agent.entrypoint import PING_HEALTHY, create_app


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


def _wait_idle(app, timeout_s: float = 10.0) -> None:
    """Poll /ping until the background run finishes (healthy again)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _s, b = _call(app, "GET", "/ping")
        if b["status"] == PING_HEALTHY:
            return
        time.sleep(0.05)


def test_ping_healthy_when_idle() -> None:
    app = create_app(mode="fake")
    status, body = _call(app, "GET", "/ping")
    assert status.startswith("200")
    assert body["status"] == PING_HEALTHY


def test_invocations_async_returns_accepted_fast() -> None:
    app = create_app(mode="fake")
    t0 = time.monotonic()
    status, body = _call(app, "POST", "/invocations",
                         json.dumps({"run_id": "contract-async-1"}).encode())
    elapsed = time.monotonic() - t0
    assert status.startswith("202")
    assert body["run_id"] == "contract-async-1"
    assert body["status"] in {"accepted", "already_running"}
    assert elapsed < 2.0, "accepted response must return quickly"
    _wait_idle(app)


def test_invocations_wait_true_is_synchronous_full_shape() -> None:
    app = create_app(mode="fake")
    payload = json.dumps({"run_id": "contract-sync-1", "wait": True}).encode()
    status, body = _call(app, "POST", "/invocations", payload)
    assert status.startswith("200")
    for key in ("run_id", "status", "stage", "artifacts", "metrics"):
        assert key in body, f"missing contract field: {key}"
    assert body["run_id"] == "contract-sync-1"
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
    _wait_idle(app)
