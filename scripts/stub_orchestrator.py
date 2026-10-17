"""Stub orchestrator: POST the exact payload the real orchestrator will send.

Used to contract-test the container locally (spec 7.6): start the agent HTTP app
(``python -m kb_agent.entrypoint --serve --mode fake --port 8080`` or the
container), then run this script to hit GET /ping and POST /invocations and assert
the response shape. Pure standard library so it needs nothing extra.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - localhost only
        return resp.status, json.loads(resp.read())


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - localhost only
        return resp.status, json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Contract-test the agent HTTP app.")
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--run-id", default="stub-orchestrator-run")
    args = parser.parse_args()

    ping_status, ping_body = _get(f"{args.base}/ping")
    print(f"GET /ping -> {ping_status} {ping_body}")
    if ping_status != 200 or ping_body.get("status") != "healthy":
        print("ping failed", file=sys.stderr)
        return 1

    payload = {"run_id": args.run_id}
    inv_status, inv_body = _post(f"{args.base}/invocations", payload)
    print(f"POST /invocations -> {inv_status}")
    print(json.dumps(inv_body, indent=2)[:800])

    required = {"run_id", "status", "stage", "artifacts", "metrics"}
    missing = required - set(inv_body)
    if inv_status != 200 or missing:
        print(f"contract check failed: status={inv_status} missing={missing}", file=sys.stderr)
        return 1
    print("\nContract OK: /ping healthy and /invocations returned the AgentOutput shape.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
