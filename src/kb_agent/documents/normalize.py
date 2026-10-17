"""normalize_docs: dedupe + metadata sidecars for KB ingestion (spec 4.2).

After validation and (where needed) BDA, each document's clean text is written to
the KB data-source prefix together with a metadata sidecar. The sidecar format is
the one CONFIRMED in Part A: a file named ``<document-filename>.metadata.json``
containing ``{"metadataAttributes": { ... }}`` (see OPEN_QUESTIONS.md log item 2).

``source_type`` matters downstream (spec 4.2): groundedness evaluation and traces
must tell official uploaded content from ticket-derived drafted content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from kb_agent.aws.interfaces import ObjectStore
from kb_agent.logging_setup import get_logger
from kb_agent.models import DocumentMetadata, Route, SourceType

logger = get_logger(__name__)


class NormalizedDocument:
    """A document ready for ingestion: its KB key, text, and sidecar key."""

    def __init__(self, doc_key: str, sidecar_key: str, content_hash: str) -> None:
        self.doc_key = doc_key
        self.sidecar_key = sidecar_key
        self.content_hash = content_hash


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sidecar_payload(metadata: DocumentMetadata) -> dict:
    """Build the confirmed ``.metadata.json`` payload (Part A item 2).

    All values are strings/bools per the KB metadata contract.
    """
    return {
        "metadataAttributes": {
            "source": metadata.source,
            "source_type": metadata.source_type.value,
            "route": metadata.route.value,
            "content_hash": metadata.content_hash,
            "run_id": metadata.run_id,
            "ingested_at": metadata.ingested_at.isoformat(),
            "language": metadata.language or "unknown",
        }
    }


def normalize_documents(
    docs: list[tuple[str, str, Route]],
    *,
    run_id: str,
    store: ObjectStore,
    data_source_prefix: str,
    source_type: SourceType = SourceType.UPLOADED_DOC,
) -> list[NormalizedDocument]:
    """Deduplicate by content hash and write docs + sidecars to the KB prefix.

    Args:
        docs: list of ``(source_name, clean_text, route)`` tuples.
        run_id: current run id (recorded in every sidecar).
        store: object store to write into.
        data_source_prefix: the KB data-source prefix (the one the KB CAN read).
        source_type: uploaded_doc or ticket_derived.

    Returns:
        One :class:`NormalizedDocument` per unique document (duplicates skipped).
    """
    seen_hashes: set[str] = set()
    results: list[NormalizedDocument] = []
    now = datetime.now(UTC)

    for source_name, text, route in docs:
        chash = _content_hash(text)
        if chash in seen_hashes:
            logger.info("normalize_dedupe_skip", source=source_name, content_hash=chash[:12])
            continue
        seen_hashes.add(chash)

        safe_name = source_name.replace("/", "_")
        doc_key = f"{data_source_prefix}/{safe_name}.md"
        sidecar_key = f"{doc_key}.metadata.json"

        metadata = DocumentMetadata(
            source=source_name,
            source_type=source_type,
            route=route,
            content_hash=chash,
            run_id=run_id,
            ingested_at=now,
            language=None,
        )

        store.put_text(doc_key, text)
        store.put_text(sidecar_key, json.dumps(sidecar_payload(metadata), indent=2))
        results.append(NormalizedDocument(doc_key, sidecar_key, chash))

    logger.info("normalize_complete", unique=len(results), input=len(docs))
    return results
