#!/usr/bin/env python3
"""Create (or recreate) the Bedrock AgentCore Runtime for the KB Integration Agent.

Uses boto3 (venv has botocore 1.43 with the bedrock-agentcore-control model; the
system `aws` CLI binary is older and lacks these commands).

Credentials come from the environment (export them via `set -a; source .env`).
The container itself gets its AWS creds from the execution role, NOT from these.
"""

from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

# Everything account/resource specific comes from the environment so no account
# IDs or ARNs live in the repo. Export them first (e.g. `set -a; source .env`).
REGION = os.environ.get("KB_AGENT_AWS_REGION", "us-east-1")
ECR_IMAGE_URI = os.environ["KB_AGENT_ECR_IMAGE_URI"]  # e.g. <acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
RUNTIME_ROLE_ARN = os.environ["KB_AGENT_RUNTIME_ROLE_ARN"]  # the AgentCore execution role
RUNTIME_NAME = os.environ.get("KB_AGENT_RUNTIME_NAME", "kb_integration_agent")

# Non-secret per-deployment config the container reads (Settings env_prefix=KB_AGENT_).
# Pulled straight from the current environment so this file carries no ARNs.
ENV_VARS = {
    "KB_AGENT_AWS_REGION": REGION,
    "KB_AGENT_S3_BUCKET": os.environ["KB_AGENT_S3_BUCKET"],
    "KB_AGENT_EMBEDDING_MODEL_ID": os.environ.get(
        "KB_AGENT_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
    ),
    "KB_AGENT_LLM_MODEL_ID": os.environ["KB_AGENT_LLM_MODEL_ID"],
    "KB_AGENT_GUARDRAIL_ENABLED": os.environ.get("KB_AGENT_GUARDRAIL_ENABLED", "false"),
    "KB_AGENT_KB_ROLE_ARN": os.environ["KB_AGENT_KB_ROLE_ARN"],
    "KB_AGENT_VECTOR_INDEX_ARN": os.environ["KB_AGENT_VECTOR_INDEX_ARN"],
    "KB_AGENT_BDA_PROJECT_ARN": os.environ["KB_AGENT_BDA_PROJECT_ARN"],
    "KB_AGENT_LOG_LEVEL": os.environ.get("KB_AGENT_LOG_LEVEL", "INFO"),
}


def main() -> int:
    c = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # If a runtime with this name already exists, report its ARN and stop.
    existing = None
    try:
        for page in c.get_paginator("list_agent_runtimes").paginate():
            for rt in page.get("agentRuntimes", []):
                if rt.get("agentRuntimeName") == RUNTIME_NAME:
                    existing = rt
                    break
    except ClientError as exc:
        print(f"WARN list_agent_runtimes failed: {exc}", file=sys.stderr)

    if existing:
        print("ALREADY_EXISTS")
        print("agentRuntimeArn:", existing.get("agentRuntimeArn"))
        print("status:", existing.get("status"))
        return 0

    resp = c.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="KB Integration Agent worker (document red-pill + ticket blue-pill).",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": ECR_IMAGE_URI}},
        roleArn=RUNTIME_ROLE_ARN,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=ENV_VARS,
    )
    print("CREATED")
    print("agentRuntimeArn:", resp.get("agentRuntimeArn"))
    print("agentRuntimeId:", resp.get("agentRuntimeId"))
    print("status:", resp.get("status"))
    print("workloadIdentity:", (resp.get("workloadIdentityDetails") or {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
