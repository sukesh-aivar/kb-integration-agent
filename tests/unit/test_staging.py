"""Tests for document staging allow/deny (spec 4.2 - KB built from content only)."""

from __future__ import annotations

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.documents.staging import is_stageable_document
from kb_agent.pipeline import Pipeline

CONFIG = load_config()


def test_allowed_content_types_are_stageable() -> None:
    for name in ("manual.md", "faq.txt", "guide.html", "scan.pdf", "policy.docx"):
        assert is_stageable_document(name) is True


def test_bookkeeping_files_are_denied() -> None:
    for name in ("README.md", "readme.md", "LICENSES.md", "LICENSE", ".gitkeep"):
        assert is_stageable_document(name) is False


def test_unknown_types_are_denied() -> None:
    for name in ("notes.rst", "data.csv", "image.png", "archive.zip", "script.py"):
        assert is_stageable_document(name) is False


def test_path_components_are_handled() -> None:
    assert is_stageable_document("sample_pack/documents/README.md") is False
    assert is_stageable_document("uploads/r1/docs/manual.md") is True


def test_pipeline_does_not_ingest_readme() -> None:
    # End-to-end: pass a README URI alongside a real doc; only the doc is ingested.
    cs = build_fake_client_set()
    cs.store.put_text("uploads/r/docs/README.md", "This directory holds documents.")
    cs.store.put_text("uploads/r/docs/manual.md", "How to reset your password: open settings.")
    ai = AgentInput(
        run_id="r",
        document_uris=[
            "s3://fake-bucket/uploads/r/docs/README.md",
            "s3://fake-bucket/uploads/r/docs/manual.md",
        ],
    )
    Pipeline(cs, CONFIG).run(ai, golden=[])

    # Only manual.md should have been normalised into the KB prefix.
    kb_docs = [
        k for k in cs.store.list_keys("runs/r/kb-docs") if k.endswith(".md")
    ]
    assert any("manual" in k for k in kb_docs)
    assert not any("README" in k for k in kb_docs)
