"""Unit tests for BDA cache, normalize sidecars, and the KB stage (spec 4.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.documents.bda import BdaProcessor, content_cache_key
from kb_agent.documents.knowledge_base import KnowledgeBaseStage, recall_at_5_from_golden
from kb_agent.documents.normalize import normalize_documents, sidecar_payload
from kb_agent.models import DocumentMetadata, Route, SourceType

CONFIG = load_config()


# --- BDA content-hash cache --------------------------------------------------


def test_cache_key_changes_with_config() -> None:
    data = b"same bytes"
    k1 = content_cache_key(data, {"format": "markdown"})
    k2 = content_cache_key(data, {"format": "html"})
    assert k1 != k2  # config change must invalidate the cache


def test_bda_second_call_is_cache_hit() -> None:
    cs = build_fake_client_set()
    # Seed the input object the fake BDA will read.
    cs.store.put_text("uploads/doc1", "page one text\fpage two text")
    proc = BdaProcessor(cs.bda, cs.store)

    data = b"page one text\fpage two text"
    first = proc.process("doc1", data, "s3://fake-bucket/uploads/doc1")
    assert first.cached is False
    assert first.pages_processed >= 1

    second = proc.process("doc1", data, "s3://fake-bucket/uploads/doc1")
    assert second.cached is True
    assert second.pages_processed == 0  # no new paid pages
    assert second.markdown == first.markdown


# --- normalize + sidecars ----------------------------------------------------


def test_sidecar_payload_matches_confirmed_format() -> None:
    md = DocumentMetadata(
        source="manual.pdf",
        source_type=SourceType.UPLOADED_DOC,
        route=Route.BDA,
        content_hash="abc",
        run_id="run-1",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        language="en",
    )
    payload = sidecar_payload(md)
    # Confirmed Part A format: top-level metadataAttributes object.
    assert "metadataAttributes" in payload
    attrs = payload["metadataAttributes"]
    assert attrs["source_type"] == "uploaded_doc"
    assert attrs["run_id"] == "run-1"


def test_normalize_dedupes_and_writes_sidecars() -> None:
    cs = build_fake_client_set()
    docs = [
        ("a.txt", "identical body", Route.DEFAULT),
        ("b.txt", "identical body", Route.DEFAULT),  # duplicate content
        ("c.txt", "different body", Route.DEFAULT),
    ]
    out = normalize_documents(
        docs, run_id="run-1", store=cs.store, data_source_prefix="kb/run-1/docs"
    )
    assert len(out) == 2  # one duplicate removed

    # Every doc has a <filename>.metadata.json sidecar in the confirmed format.
    for nd in out:
        assert nd.sidecar_key.endswith(".md.metadata.json")
        payload = json.loads(cs.store.get_text(nd.sidecar_key))
        assert "metadataAttributes" in payload


# --- KB stage end to end (fake) ----------------------------------------------


def test_kb_create_ingest_retrieve_roundtrip() -> None:
    cs = build_fake_client_set()
    prefix = "kb/run-1/docs"
    cs.store.put_text(
        f"{prefix}/password.md",
        "To reset your password, open account settings and click reset password.",
    )
    cs.store.put_text(
        f"{prefix}/sync.md",
        "If the desktop client will not sync, clear the local sync journal and relink.",
    )

    stage = KnowledgeBaseStage(cs.knowledge_base, CONFIG)
    stage.create("test-kb", prefix)
    # ingest() now returns the document-level ingestion stats dict when the
    # client exposes them (spec v2 §5.5); the fake indexes both docs.
    stats = stage.ingest()
    assert isinstance(stats, dict)
    assert stats["indexed"] == 2
    assert stats["failed"] == 0

    passages = stage.retrieve("how do I reset my password", top_k=5)
    assert passages
    assert "password" in passages[0].source_id  # best match is the password doc


def test_recall_at_5_from_golden_scores() -> None:
    cs = build_fake_client_set()
    prefix = "kb/run-1/docs"
    cs.store.put_text(f"{prefix}/reset.md", "reset your password from account settings")
    stage = KnowledgeBaseStage(cs.knowledge_base, CONFIG)
    stage.create("kb", prefix)
    stage.ingest()

    golden = [
        {"id": "q1", "question": "how to reset my password", "expected_source_ids": ["reset.md"]},
        {
            "id": "q2",
            "question": "unrelated nonsense query zzz",
            "expected_source_ids": ["missing.md"],
        },
    ]
    recall, detail = recall_at_5_from_golden(stage, golden, k=5)
    assert recall == 0.5  # one hit, one miss
    assert detail[0]["hit"] is True
    assert detail[1]["hit"] is False
