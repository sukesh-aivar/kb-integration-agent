"""Tests for the seeded PII document fixture + adversarial PII cases (condition 4,5)."""

from __future__ import annotations

from pathlib import Path

import yaml

from kb_agent.aws.fakes import FakeGuardrail
from kb_agent.config import load_config
from kb_agent.documents.pii import regex_pii_findings
from kb_agent.documents.validate import validate_text

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "sample_pack" / "validation_fixtures"
CONFIG = load_config()
GUARDRAIL = FakeGuardrail()


def test_seeded_pii_document_is_rejected() -> None:
    text = (FIXTURES / "pii_document.md").read_text()
    res = validate_text("pii_document.md", text, CONFIG, GUARDRAIL)
    assert not res.passed
    assert res.pii_findings
    # The seeded document contains all four PII kinds.
    kinds = {f.kind for f in res.pii_findings}
    assert {"email", "phone", "card", "national_id"} <= kinds


def test_clean_document_is_not_rejected() -> None:
    text = (FIXTURES / "clean_document.md").read_text()
    res = validate_text("clean_document.md", text, CONFIG, GUARDRAIL)
    assert res.passed
    assert not res.pii_findings


def test_adversarial_expected_cases_are_detected() -> None:
    # Cases marked expect_detected: true must be caught by the regex layer.
    cases = yaml.safe_load((REPO_ROOT / "evals" / "pii_adversarial.yaml").read_text())["cases"]
    for c in cases:
        if c["expect_detected"]:
            found = bool(regex_pii_findings(c["text"]))
            assert found, f"regex should detect PII in adversarial case: {c['name']}"
