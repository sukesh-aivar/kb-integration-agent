"""Held-out routing test on the REAL fetched documents (approval condition 5).

Written after the router + rules, using the actual document files, so a rule
table over-fit to the original synthetic fixtures would be exposed here. Skips if
the documents have not been fetched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kb_agent.config import load_config, load_routing_rules
from kb_agent.documents.features import extract_features
from kb_agent.documents.router import route_document

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "sample_pack" / "documents"
FIXTURES = REPO_ROOT / "evals" / "routing_fixtures_heldout.yaml"
CONFIG = load_config()
RULES = load_routing_rules()

_fixtures = yaml.safe_load(FIXTURES.read_text())["fixtures"]
_present = [fx for fx in _fixtures if (DOCS / fx["path"]).exists()]


@pytest.mark.skipif(not _present, reason="fetched documents not present")
@pytest.mark.parametrize("fx", _present, ids=lambda fx: fx["name"])
def test_heldout_real_document_routes_as_expected(fx: dict) -> None:
    path = DOCS / fx["path"]
    features = extract_features(path.name, path.read_bytes())
    decision = route_document(features, CONFIG, RULES)
    assert decision.route.value == fx["expected_route"], (
        f"{fx['path']}: expected {fx['expected_route']}, got {decision.route.value}"
    )
