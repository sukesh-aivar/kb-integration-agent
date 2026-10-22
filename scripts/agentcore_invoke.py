#!/usr/bin/env python3
"""Invoke the deployed KB Integration Agent on Bedrock AgentCore Runtime (spec v2).

The deployed agent is ASYNC: ``POST /invocations`` returns ``202 {run_id, status:
accepted}`` almost immediately and runs the pipeline on a background thread,
writing live progress to ``s3://<bucket>/runs/{run_id}/``. This script therefore:

  1. sends the AgentInput (async by default),
  2. then POLLS ``runs/{run_id}/_finished.json`` in S3 until the run settles,
     printing new ``_events`` as they appear.

Pass ``--wait`` to use the synchronous path instead (the agent blocks and returns
the full AgentOutput on the same call) — only useful for short runs within the
socket timeout.

Environment (export via `set -a; source .env`):
    KB_AGENT_AWS_REGION        (default us-east-1)
    KB_AGENT_RUNTIME_ARN       the deployed AgentCore runtime ARN
    KB_AGENT_S3_BUCKET         bucket the agent writes run state to (for polling)
    KB_AGENT_TEST_DOCUMENT_URI optional sample document s3:// uri
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("KB_AGENT_AWS_REGION", "us-east-1")
RUNTIME_ARN = os.environ["KB_AGENT_RUNTIME_ARN"]
S3_BUCKET = os.environ.get("KB_AGENT_S3_BUCKET")
DOCUMENT_URI = os.environ.get(
    "KB_AGENT_TEST_DOCUMENT_URI", "s3://<S3_BUCKET>/sample-doc-runtime.pdf"
)


def _invoke(payload: dict, *, wait: bool) -> dict:
    """Send the AgentInput; return the parsed JSON body the runtime returns."""
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        # Sync runs can take a while; async returns immediately but keep a margin.
        config=Config(
            read_timeout=870 if wait else 60,
            connect_timeout=15,
            retries={"max_attempts": 0},
        ),
    )
    session_id = f"kb-agent-session-{uuid.uuid4().hex}"  # must be >= 33 chars
    body = dict(payload)
    if wait:
        body["wait"] = True
    print(f"invoking run_id={payload['run_id']} session={session_id} wait={wait}")
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(body).encode("utf-8"),
    )
    print("statusCode:", resp.get("statusCode"))
    raw = resp["response"].read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("RAW:", raw[:2000])
        return {}


def _poll_until_finished(run_id: str, *, interval_s: float, timeout_s: float) -> int:
    """Poll S3 for runs/{run_id}/_finished.json, streaming new events meanwhile."""
    if not S3_BUCKET:
        print(
            "WARN: KB_AGENT_S3_BUCKET not set; cannot poll run state. "
            "Set it to watch async progress.",
            file=sys.stderr,
        )
        return 0
    s3 = boto3.client("s3", region_name=REGION)
    prefix = f"runs/{run_id}"
    finished_key = f"{prefix}/_finished.json"
    seen_events: set[str] = set()
    deadline = time.monotonic() + timeout_s

    while True:
        # Stream any new event files (sorted so order is stable).
        try:
            page = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{prefix}/_events/")
            for key in sorted(o["Key"] for o in page.get("Contents", [])):
                if key in seen_events:
                    continue
                seen_events.add(key)
                try:
                    ev = json.loads(
                        s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
                    )
                    stage = ev.get("stage") or "-"
                    print(
                        f"  [{ev.get('t', 0):>4}s] {ev.get('type'):<13} "
                        f"{stage:<16} {ev.get('text', '')}"
                    )
                except ClientError:
                    pass
        except ClientError as exc:
            print(f"WARN list events failed: {exc}", file=sys.stderr)

        # Check for the terminal marker.
        try:
            fin = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=finished_key)["Body"].read())
            print("\n_finished.json:")
            print(json.dumps(fin, indent=2))
            return 0 if fin.get("status") == "needs_review" else 1
        except ClientError:
            pass  # not finished yet

        if time.monotonic() > deadline:
            print(f"\nTIMEOUT: run {run_id} did not finish within {timeout_s}s.", file=sys.stderr)
            return 2
        time.sleep(interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", default=f"agentcore-rt-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--wait", action="store_true", help="Use the synchronous path.")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--poll-timeout", type=float, default=1800.0)
    args = parser.parse_args()

    payload = {"run_id": args.run_id, "document_uris": [DOCUMENT_URI]}

    result = _invoke(payload, wait=args.wait)
    print(json.dumps(result, indent=2))

    if args.wait:
        # Synchronous: the body IS the AgentOutput.
        return 0 if result.get("status") == "needs_review" else 1

    # Async: expect {run_id, status: accepted}. Then poll S3.
    status = result.get("status")
    if status not in {"accepted", "already_running"}:
        print(f"unexpected async response status: {status!r}", file=sys.stderr)
        return 1
    run_id = result.get("run_id", args.run_id)
    print(f"\naccepted; polling s3://{S3_BUCKET}/runs/{run_id}/ ...")
    return _poll_until_finished(
        run_id, interval_s=args.poll_interval, timeout_s=args.poll_timeout
    )


if __name__ == "__main__":
    raise SystemExit(main())
