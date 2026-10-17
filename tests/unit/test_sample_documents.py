"""Guard tests for the fetched sample documents (spec 1.4, 4.2).

Only run if documents have been fetched (sample_pack/fetch_documents.py). They
assert the real documents route as intended - text to DEFAULT, scanned PDFs to
BDA - so the router is exercised on genuine files, not just synthetic fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_agent.config import load_config, load_routing_rules
from kb_agent.documents.features import extract_features
from kb_agent.documents.router import route_document
from kb_agent.models import Route

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "sample_pack" / "documents"
CONFIG = load_config()
RULES = load_routing_rules()

_text_docs = sorted(
    p
    for p in DOCS.glob("*")
    if p.suffix in {".md", ".txt", ".html"} and p.name != "README.md"
)
_pdf_docs = sorted(DOCS.glob("*.pdf"))


@pytest.mark.skipif(not _text_docs, reason="no fetched documents (run fetch_documents.py)")
@pytest.mark.parametrize("path", _text_docs, ids=lambda p: p.name)
def test_text_documents_route_default(path: Path) -> None:
    features = extract_features(path.name, path.read_bytes())
    decision = route_document(features, CONFIG, RULES)
    assert decision.route == Route.DEFAULT


@pytest.mark.skipif(not _pdf_docs, reason="no scanned PDF fixtures")
@pytest.mark.parametrize("path", _pdf_docs, ids=lambda p: p.name)
def test_scanned_pdfs_route_bda(path: Path) -> None:
    features = extract_features(path.name, path.read_bytes())
    decision = route_document(features, CONFIG, RULES)
    # Image-only (scanned-style) PDFs have ~0 extracted chars -> BDA (rule 6).
    assert decision.route == Route.BDA
