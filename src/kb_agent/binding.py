"""The handover contract: knowledge_binding.json (Part B B5.3).

This is the product's primary output. A downstream agent (retrieval, evaluation,
a chat UI) binds to a project's knowledge by reading this one record rather than
copying ids around: it carries the knowledge base id/arn, the vector store
coordinates (bucket, index, dimension, metric), the embedding model, the chunking
config, the source prefix, the ingestion result, and the per-file document list.

Why more than the KB id: calling retrieval needs the region; any further
ingestion needs the embedding model + dimension to match the (immutable) index;
an evaluation agent needs the retrieval configuration. A logical "project's
knowledge" record lets artifacts refer to it instead of duplicating ids.

Written to two locations (B5.3):
* ``runs/{run_id}/knowledge_binding.json``      - this run's immutable record
* ``projects/{project_id}/knowledge_binding.json`` - the project's latest

Content rule: names, ids, counts and statuses only - never document or row text.
"""

from __future__ import annotations

import json
from typing import Any

from kb_agent.aws.interfaces import ObjectStore
from kb_agent.documents.outcomes import FileOutcome
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)

BINDING_CONTRACT_VERSION = "1.0"


def build_binding(
    *,
    project_id: str,
    run_id: str,
    created_at: str,
    region: str,
    account_id: str | None,
    status: str,
    knowledge_base: dict[str, Any],
    vector_store: dict[str, Any],
    embedding_model_id: str,
    chunking: dict[str, Any],
    source: dict[str, Any],
    ingestion: dict[str, Any],
    outcomes: list[FileOutcome],
) -> dict[str, Any]:
    """Assemble the versioned binding record (pure; no I/O)."""
    documents = [
        {
            "name": o.name,
            "format": o.format,
            "route": o.route,
            "status": o.status,
            "status_reason": o.status_reason,
            "extraction_uri": o.extraction_uri,
        }
        for o in outcomes
    ]
    return {
        "contract_version": BINDING_CONTRACT_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "created_at": created_at,
        "status": status,
        "region": region,
        "account_id": account_id,
        "knowledge_base": knowledge_base,
        "vector_store": vector_store,
        "embedding_model_id": embedding_model_id,
        "chunking": chunking,
        "source": source,
        "ingestion": ingestion,
        "documents": documents,
    }


def write_binding(store: ObjectStore, binding: dict[str, Any]) -> str:
    """Write the binding to the run and project locations; return the RUN uri.

    The run copy is the stable per-run record; the project copy is overwritten
    each run as "latest". Both are a single ``put_text`` (atomic read for the
    reader). Returns the run-copy uri (what AgentOutput.knowledge_binding_uri
    points at).
    """
    body = json.dumps(binding, indent=2)
    run_id = binding["run_id"]
    project_id = binding["project_id"]
    run_uri = store.put_text(f"runs/{run_id}/knowledge_binding.json", body)
    try:
        store.put_text(f"projects/{project_id}/knowledge_binding.json", body)
    except Exception as exc:  # noqa: BLE001 - the project "latest" copy is best-effort
        logger.warning("binding_project_copy_failed", project_id=project_id, error=str(exc))
    return run_uri
