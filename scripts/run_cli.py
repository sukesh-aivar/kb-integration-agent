"""freq-test CLI: run the agent on S3 URIs you pass in (not the sample pack).

.. note::

    DORMANT-DEPENDENT dev script (v3 Part A). Developer CLI, not shipped in the
    runtime image and not imported by the active agent. It accepts a
    ``--ticket-uri`` and can drive the legacy ticket flow, so it may depend on
    the parked ``dormant/`` code and ``requirements-dormant.txt`` packages.
    Left in place unchanged in Part A; its alignment to the KB-only workflow is
    deferred to Part B.

Point it at a document and/or ticket CSV already sitting in your S3 bucket, pick
the mode, and it runs the full pipeline and prints a clear final summary.

Examples
--------
    # a PDF already uploaded to the bucket, real AWS:
    python scripts/run_cli.py --mode aws \
        --document-uri s3://kb-integration-agent-test/uploads/test/mydoc.pdf

    # a document + a ticket CSV:
    python scripts/run_cli.py --mode aws \
        --document-uri s3://.../mydoc.pdf --ticket-uri s3://.../tickets.csv

The S3 object(s) must already exist. Credentials + resource ARNs come from the
environment / .env (see .env.example). Nothing is uploaded by this script - it
only references existing S3 objects, which matches how the orchestrator invokes
the agent (S3 references, not payloads).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kb_agent.aws.clientset import build_client_set  # noqa: E402
from kb_agent.config import get_settings, load_config  # noqa: E402
from kb_agent.contract import AgentInput  # noqa: E402
from kb_agent.logging_setup import configure_logging  # noqa: E402
from kb_agent.pipeline import Pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the KB Integration Agent on S3 URIs.")
    parser.add_argument("--mode", choices=["fake", "aws"], default="aws")
    parser.add_argument("--run-id", default="freq-test")
    parser.add_argument(
        "--document-uri",
        action="append",
        default=[],
        help="s3://bucket/key of a document to ingest (repeatable).",
    )
    parser.add_argument("--ticket-uri", default=None, help="s3://bucket/key of a ticket CSV.")
    parser.add_argument(
        "--force-bda",
        action="store_true",
        help="Force every document down the BDA route regardless of routing (test).",
    )
    args = parser.parse_args()

    settings = get_settings()
    config = load_config()
    configure_logging(settings.log_level)

    if not args.document_uri and not args.ticket_uri:
        parser.error("provide at least one of --document-uri or --ticket-uri")

    clients = build_client_set(args.mode, settings, config)

    agent_input = AgentInput(
        run_id=args.run_id,
        document_uris=args.document_uri,
        ticket_uri=args.ticket_uri,
    )
    output = Pipeline(clients, config, force_bda=args.force_bda).run(agent_input)

    print("\n=== RUN COMPLETE ===")
    print(f"status : {output.status}")
    print(f"stage  : {output.stage}")
    m = output.metrics
    print(
        f"metrics: documents={m.document_count} tickets={m.ticket_count} "
        f"clusters={m.cluster_count} gaps_flagged={m.gaps_flagged}"
    )
    if output.artifacts:
        print("artifacts:")
        for name, uri in output.artifacts.items():
            print(f"  {name}: {uri}")
    if output.errors:
        print("errors:")
        for e in output.errors:
            print(f"  [{e.stage}] {e.kind}: {e.message}")
    print("NOTE: metrics on synthetic/test data; see the report + manifest in S3.")
    return 0 if output.status in {"needs_review", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
