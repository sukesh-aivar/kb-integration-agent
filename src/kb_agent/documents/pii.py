"""Regex PII layer (spec 4.2 validate Stage B, 4.4 drafting re-scan).

This is the deterministic, offline half of PII detection: it catches
format-obvious cases cheaply (emails, phones, card-like numbers, national IDs).
Bedrock Guardrails is the contextual half, applied alongside it in validation and
drafting. The spec is explicit that both run (spec 4.2): regex for cheap obvious
hits, Guardrails for contextual ones.

Pure and offline so it runs in unit tests and in ``--mode fake`` with no AWS.
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class RegexPiiFinding(BaseModel):
    """A single regex PII hit. ``masked_sample`` never contains the raw value."""

    kind: str
    masked_sample: str


# Patterns are intentionally conservative: better to flag for human review than
# to miss. Card detection uses a Luhn check to cut obvious false positives.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?){2,4}\d{2,4}(?!\d)")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_NATIONAL_ID_RE = re.compile(r"\b[A-Z]{2}-?\d{6}-?[A-Z]\b")


def _mask(value: str) -> str:
    """Mask a detected value, keeping only a short, non-identifying hint."""
    cleaned = value.strip()
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return f"{cleaned[:2]}{'*' * (len(cleaned) - 4)}{cleaned[-2:]}"


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum - used to reduce card false positives."""
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    total = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def regex_pii_findings(text: str) -> list[RegexPiiFinding]:
    """Return all regex PII findings in ``text`` (empty == clean).

    Order is deterministic (emails, phones, cards, national IDs) so results are
    reproducible across runs.
    """
    findings: list[RegexPiiFinding] = []

    for m in _EMAIL_RE.finditer(text):
        findings.append(RegexPiiFinding(kind="email", masked_sample=_mask(m.group())))

    # Cards first so a card number is not also reported as a phone number.
    card_spans: list[tuple[int, int]] = []
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if _luhn_ok(digits):
            findings.append(RegexPiiFinding(kind="card", masked_sample=_mask(digits)))
            card_spans.append(m.span())

    for m in _PHONE_RE.finditer(text):
        # Skip if this span overlaps a detected card number.
        if any(s <= m.start() < e or s < m.end() <= e for s, e in card_spans):
            continue
        digits = re.sub(r"\D", "", m.group())
        if 7 <= len(digits) <= 15:
            findings.append(RegexPiiFinding(kind="phone", masked_sample=_mask(m.group())))

    for m in _NATIONAL_ID_RE.finditer(text):
        findings.append(RegexPiiFinding(kind="national_id", masked_sample=_mask(m.group())))

    return findings


def contains_pii(text: str) -> bool:
    """Convenience predicate: does the regex layer flag any PII?"""
    return bool(regex_pii_findings(text))
