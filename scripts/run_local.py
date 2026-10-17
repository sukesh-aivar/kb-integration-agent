"""Local pipeline runner (spec 10 step 7; `make run-fake` / `make run-aws`).

Stages the sample pack into the chosen client set and runs the full pipeline. In
``--mode fake`` everything is in-memory and offline; in ``--mode aws`` real
services are used (and the sample pack is uploaded to S3 first). Prints the
resulting report location and headline metrics.

This script is intentionally thin - all logic lives in the pipeline and stages.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kb_agent.aws.clientset import build_client_set  # noqa: E402
from kb_agent.config import get_settings, load_config  # noqa: E402
from kb_agent.contract import AgentInput  # noqa: E402
from kb_agent.logging_setup import configure_logging  # noqa: E402
from kb_agent.pipeline import Pipeline  # noqa: E402

SAMPLE_PACK = REPO_ROOT / "sample_pack"
EVALS = REPO_ROOT / "evals"


def _load_golden() -> list[dict]:
    """Load the golden set rows (skipping the schema/_comment line)."""
    rows: list[dict] = []
    with (EVALS / "golden_set.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "_comment" in obj:
                continue
            rows.append(obj)
    return rows


def _stage_sample_documents(store, run_id: str) -> list[str]:
    """Place any sample documents into the store; return their URIs.

    The sample_pack/documents directory is empty by default (no fabricated
    content), so this usually returns no document URIs - the pipeline then runs
    the ticket path only, which is valid (spec 6: skip inapplicable stages).
    """
    from kb_agent.documents.staging import ALLOWED_DOC_SUFFIXES, is_stageable_document

    uris: list[str] = []
    docs_dir = SAMPLE_PACK / "documents"
    for path in sorted(docs_dir.glob("**/*")):
        if not path.is_file() or not is_stageable_document(path.name):
            continue
        key = f"uploads/{run_id}/docs/{path.name}"
        if path.suffix.lower() in {".pdf"}:
            store.put_bytes(key, path.read_bytes())
        else:
            store.put_text(key, path.read_text(encoding="utf-8"))
        uris.append(f"s3://{getattr(store, '_bucket', 'bucket')}/{key}")
    _ = ALLOWED_DOC_SUFFIXES  # referenced for clarity of intent
    return uris


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the KB Integration Agent locally.")
    parser.add_argument("--mode", choices=["fake", "aws"], default="fake")
    parser.add_argument("--run-id", default="local-run")
    args = parser.parse_args()

    settings = get_settings()
    config = load_config()
    configure_logging(settings.log_level)

    clients = build_client_set(args.mode, settings, config)

    # Stage the sample tickets into the store.
    tickets_csv = (SAMPLE_PACK / "tickets" / "tickets.csv").read_bytes()
    ticket_key = f"uploads/{args.run_id}/tickets.csv"
    clients.store.put_bytes(ticket_key, tickets_csv)
    bucket = getattr(clients.store, "_bucket", "bucket")
    ticket_uri = f"s3://{bucket}/{ticket_key}"

    document_uris = _stage_sample_documents(clients.store, args.run_id)

    agent_input = AgentInput(
        run_id=args.run_id, ticket_uri=ticket_uri, document_uris=document_uris
    )
    output = Pipeline(clients, config).run(agent_input, golden=_load_golden())

    print("\n=== RUN COMPLETE ===")
    print(f"status: {output.status}  stage: {output.stage}")
    print(f"artifacts: {json.dumps(output.artifacts, indent=2)}")
    m = output.metrics
    print(
        "metrics: "
        f"tickets={m.ticket_count} clusters={m.cluster_count} "
        f"noise_share={m.noise_share} recall@5={m.recall_at_5} "
        f"gaps_flagged={m.gaps_flagged}"
    )
    print("NOTE: all metrics are measured on SYNTHETIC data.")
    return 0 if output.status in {"needs_review", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
