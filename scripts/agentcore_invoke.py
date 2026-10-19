#!/usr/bin/env python3
"""Invoke the deployed KB Integration Agent on Bedrock AgentCore Runtime.

Sends an AgentInput payload to the runtime's /invocations endpoint via the
AgentCore data plane and prints the AgentOutput it returns.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
from botocore.config import Config

# Account/resource specific values come from the environment (no ARNs in the repo).
REGION = os.environ.get("KB_AGENT_AWS_REGION", "us-east-1")
RUNTIME_ARN = os.environ["KB_AGENT_RUNTIME_ARN"]  # the deployed AgentCore runtime ARN
DOCUMENT_URI = os.environ.get(
    "KB_AGENT_TEST_DOCUMENT_URI", "s3://<S3_BUCKET>/sample-doc-runtime.pdf"
)


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "agentcore-rt-1"
    payload = {
        "run_id": run_id,
        "document_uris": [DOCUMENT_URI],
    }

    # The pipeline runs synchronously; give the socket plenty of time.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(read_timeout=870, connect_timeout=15, retries={"max_attempts": 0}),
    )

    # AgentCore requires runtimeSessionId to be >= 33 chars.
    session_id = f"kb-agent-session-{uuid.uuid4().hex}"

    print(f"invoking run_id={run_id} session={session_id}")
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode("utf-8"),
    )

    print("statusCode:", resp.get("statusCode"))
    print("contentType:", resp.get("contentType"))
    body = resp["response"].read()
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        print("RAW:", body[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
