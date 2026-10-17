"""Unit tests for validate_uploads stages A/B (spec 4.2, 7.5 failure/safety)."""

from __future__ import annotations

from kb_agent.aws.fakes import FakeGuardrail
from kb_agent.config import load_config
from kb_agent.documents.features import DocumentFeatures
from kb_agent.documents.validate import (
    detect_language,
    needs_escalation,
    validate_bytes,
    validate_text,
)

CONFIG = load_config()
GUARDRAIL = FakeGuardrail()


def _features(**kw) -> DocumentFeatures:
    base = dict(file_id="f", detected_mime="application/pdf", size_bytes=1000)
    base.update(kw)
    return DocumentFeatures(**base)


def test_stage_a_rejects_encrypted_pdf() -> None:
    result = validate_bytes(_features(pdf_encrypted_or_unreadable=True), CONFIG)
    assert not result.passed
    assert "encrypted_or_unreadable_pdf" in result.failures


def test_stage_a_rejects_oversized_file() -> None:
    huge = CONFIG.routing.max_file_size_mb * 1024 * 1024 + 1
    result = validate_bytes(_features(size_bytes=huge), CONFIG)
    assert not result.passed
    assert any("file_too_large" in f for f in result.failures)


def test_stage_a_passes_clean_file() -> None:
    assert validate_bytes(_features(), CONFIG).passed


def test_stage_b_flags_pii() -> None:
    result = validate_text("f", "email me at a.b@example.com", CONFIG, GUARDRAIL)
    assert not result.passed
    assert result.pii_findings
    # Both detectors run in fake mode (regex + fake guardrail reusing regex).
    assert {f.detector for f in result.pii_findings} >= {"regex"}


def test_stage_b_passes_clean_english_text() -> None:
    text = "The desktop sync client would not start after the latest update this morning."
    result = validate_text("f", text, CONFIG, GUARDRAIL)
    assert result.passed
    assert result.language == "en"


def test_non_english_flagged_not_rejected_by_default() -> None:
    # Default action is 'flag': language recorded, validation still passes.
    text = "Bonjour, je ne peux pas me connecter a mon compte depuis ce matin merci."
    result = validate_text("f", text, CONFIG, GUARDRAIL)
    assert result.language != "en"
    assert result.passed  # flagged, not rejected


def test_non_english_rejected_when_configured() -> None:
    cfg = load_config()
    cfg.validation.non_english_action = "reject"
    text = "Hola, la aplicacion se cierra sola cada vez que la abro por la manana."
    result = validate_text("f", text, cfg, GUARDRAIL)
    assert not result.passed
    assert any("non_english" in f for f in result.failures)


def test_detect_language_short_text_defaults_english() -> None:
    assert detect_language("ok") == "en"


def test_escalation_backstop_triggers_on_empty_text() -> None:
    assert needs_escalation("", CONFIG) is True


def test_escalation_backstop_passes_good_text() -> None:
    assert needs_escalation("A clean paragraph of readable English text." * 3, CONFIG) is False
