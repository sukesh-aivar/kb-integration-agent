#!/usr/bin/env python3
"""Update the AgentCore Runtime to a new container image tag (spec v2 Checkpoint 1).

The AGENT NEVER MUTATES AWS. This script defaults to DRY-RUN: it reads the current
runtime configuration, works out the single change needed (point the runtime at
the new ECR image, e.g. ``:v3``), and prints exactly what WOULD change plus the
equivalent AWS console steps. It carries the existing execution role and
environment variables over UNCHANGED.

Nothing is sent to AWS unless the HUMAN runs it with ``--apply`` themselves. The
agent is only expected to run the dry-run.

Usage (human):
    set -a; source .env; set +a
    # preview only (safe; this is all the agent runs):
    .venv/bin/python scripts/update_runtime.py --image-tag v3
    # actually update (human only):
    .venv/bin/python scripts/update_runtime.py --image-tag v3 --apply

Environment (no ARNs in the repo; export via `set -a; source .env`):
    KB_AGENT_AWS_REGION     (default us-east-1)
    KB_AGENT_RUNTIME_NAME   (default kb_integration_agent)
    KB_AGENT_ECR_IMAGE_URI  e.g. <acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
                            (its repo part is reused; the tag is replaced by --image-tag)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("KB_AGENT_AWS_REGION", "us-east-1")
RUNTIME_NAME = os.environ.get("KB_AGENT_RUNTIME_NAME", "kb_integration_agent")


def _repo_base(image_uri: str) -> str:
    """Strip an existing ``:tag`` from an ECR image URI, keeping the repo part.

    ``123.dkr.ecr.us-east-1.amazonaws.com/repo:v2`` -> ``.../repo``. Only the tag
    after the LAST colon is removed; the registry host's ``:`` (if any) is kept by
    splitting on the final path segment.
    """
    # The host never carries a port in ECR URIs, so a simple rsplit on ':' is safe
    # as long as there is a '/' after the host (there always is for a repo URI).
    if "/" in image_uri and ":" in image_uri.rsplit("/", 1)[1]:
        return image_uri.rsplit(":", 1)[0]
    return image_uri


def _find_runtime(control: object) -> dict | None:
    for page in control.get_paginator("list_agent_runtimes").paginate():  # type: ignore[attr-defined]
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == RUNTIME_NAME:
                return rt
    return None


def _print_console_steps(new_image: str) -> None:
    print()
    print("=== AWS Console steps (equivalent manual update) ===")
    print("1. Open the Bedrock AgentCore console -> Agent Runtimes.")
    print(f"2. Select the runtime named '{RUNTIME_NAME}' (region {REGION}).")
    print("3. Edit the container configuration.")
    print(f"4. Set the container image URI to:\n     {new_image}")
    print("5. Leave the execution role and environment variables UNCHANGED.")
    print("6. Save. Wait for the runtime status to return to READY.")
    print("====================================================")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-tag",
        default="v3",
        help="ECR image tag to point the runtime at (default: v3).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ACTUALLY update the runtime. Omitted = dry-run (default). "
        "The agent must never pass this; only a human runs --apply.",
    )
    args = parser.parse_args()

    base_image = os.environ.get("KB_AGENT_ECR_IMAGE_URI")
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)

    try:
        rt = _find_runtime(control)
    except ClientError as exc:
        print(f"ERROR: could not list runtimes: {exc}", file=sys.stderr)
        return 2

    if rt is None:
        print(
            f"ERROR: no AgentCore runtime named '{RUNTIME_NAME}' in {REGION}.",
            file=sys.stderr,
        )
        return 2

    runtime_id = rt.get("agentRuntimeId")
    runtime_arn = rt.get("agentRuntimeArn")

    # Read the FULL current config so env vars + role can be carried over verbatim.
    try:
        current = control.get_agent_runtime(agentRuntimeId=runtime_id)
    except ClientError as exc:
        print(f"ERROR: get_agent_runtime failed: {exc}", file=sys.stderr)
        return 2

    role_arn = current.get("roleArn")
    env_vars = current.get("environmentVariables", {}) or {}
    network = current.get("networkConfiguration", {}) or {"networkMode": "PUBLIC"}
    protocol = current.get("protocolConfiguration", {}) or {"serverProtocol": "HTTP"}
    artifact = current.get("agentRuntimeArtifact", {}) or {}
    current_image = (
        artifact.get("containerConfiguration", {}).get("containerUri", "")
    )

    # Derive the new image: reuse the repo part, swap in the requested tag.
    repo = _repo_base(base_image or current_image)
    if not repo:
        print(
            "ERROR: cannot determine ECR repo. Set KB_AGENT_ECR_IMAGE_URI or ensure "
            "the runtime already references an image.",
            file=sys.stderr,
        )
        return 2
    new_image = f"{repo}:{args.image_tag}"

    print(f"runtime name : {RUNTIME_NAME}")
    print(f"runtime arn  : {runtime_arn}")
    print(f"region       : {REGION}")
    print(f"role (keep)  : {role_arn}")
    print(f"env vars     : {len(env_vars)} carried over unchanged")
    print(f"current image: {current_image or '(unknown)'}")
    print(f"new image    : {new_image}")

    if current_image == new_image:
        print("\nNOTE: the runtime already points at this image. Nothing to change.")

    if not args.apply:
        print("\nDRY-RUN: no changes sent to AWS. Re-run with --apply to update (human only).")
        _print_console_steps(new_image)
        # Emit the exact call that --apply would make, for transparency.
        print("\nupdate_agent_runtime call that --apply would make:")
        print(
            json.dumps(
                {
                    "agentRuntimeId": runtime_id,
                    "agentRuntimeArtifact": {
                        "containerConfiguration": {"containerUri": new_image}
                    },
                    "roleArn": role_arn,
                    "networkConfiguration": network,
                    "protocolConfiguration": protocol,
                    "environmentVariables": "<carried over unchanged>",
                },
                indent=2,
            )
        )
        return 0

    # --apply path: HUMAN-ONLY. Carry role + env vars + network + protocol verbatim.
    print("\nAPPLY: updating the runtime image...")
    resp = control.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": new_image}},
        roleArn=role_arn,
        networkConfiguration=network,
        protocolConfiguration=protocol,
        environmentVariables=env_vars,
    )
    print("UPDATED")
    print("status:", resp.get("status"))
    print("Wait for the runtime status to return to READY before invoking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
