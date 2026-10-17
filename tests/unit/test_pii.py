"""Unit tests for the regex PII layer (spec 4.2 Stage B, 7.5 PII regex)."""

from __future__ import annotations

from kb_agent.documents.pii import contains_pii, regex_pii_findings


def test_detects_email() -> None:
    kinds = {f.kind for f in regex_pii_findings("contact jane.doe@example.com please")}
    assert "email" in kinds


def test_detects_valid_card_via_luhn() -> None:
    # 4111 1111 1111 1111 is a standard test card that passes Luhn.
    findings = regex_pii_findings("card 4111 1111 1111 1111 on file")
    assert any(f.kind == "card" for f in findings)


def test_rejects_non_luhn_number_as_card() -> None:
    # A random 16-digit run that fails Luhn should not be a 'card'.
    findings = regex_pii_findings("order 1234 5678 9012 3456 shipped")
    assert not any(f.kind == "card" for f in findings)


def test_detects_phone() -> None:
    findings = regex_pii_findings("call me on +1-202-555-0147 today")
    assert any(f.kind == "phone" for f in findings)


def test_detects_national_id() -> None:
    findings = regex_pii_findings("my id is AB-123456-C thanks")
    assert any(f.kind == "national_id" for f in findings)


def test_masking_hides_raw_value() -> None:
    findings = regex_pii_findings("jane.doe@example.com")
    assert findings
    for f in findings:
        assert "jane.doe@example.com" not in f.masked_sample
        assert "*" in f.masked_sample


def test_clean_text_has_no_findings() -> None:
    assert regex_pii_findings("The sync client would not start this morning.") == []
    assert contains_pii("nothing sensitive here") is False
