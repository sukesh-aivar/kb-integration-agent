"""validate_uploads: two-stage document validation (spec 4.2).

The spec's key ordering subtlety: a scanned PDF has no text layer, so there is
nothing to PII-scan until BDA has run. The rule is therefore NOT "validate then
parse" but: *no document reaches the knowledge base until its extracted text has
passed Stage B* - whether that text came from local extraction or from BDA.

So this module splits into:

* :func:`validate_bytes` - Stage A, on raw bytes, no parsing cost: true MIME,
  size cap, encrypted/corrupt PDF.
* :func:`validate_text` - Stage B, on extracted text: PII (Guardrails + regex)
  and language. Called with locally-extracted text for simple files, or with
  BDA markdown for scanned files, so the gate is enforced either way.

Rejections are flagged for review, never silently dropped (spec 4.2), and counted.
"""

from __future__ import annotations

from kb_agent.aws.interfaces import GuardrailClient
from kb_agent.config import AppConfig
from kb_agent.documents.features import DocumentFeatures
from kb_agent.documents.pii import regex_pii_findings
from kb_agent.models import PiiFinding, ValidationResult


def validate_bytes(features: DocumentFeatures, config: AppConfig) -> ValidationResult:
    """Stage A: validate raw bytes before any parsing (spec 4.2).

    Checks true MIME presence, the size cap, and encrypted/corrupt PDFs. Passing
    Stage A does NOT clear a document for the KB - Stage B on extracted text is
    still required.
    """
    failures: list[str] = []

    max_bytes = config.routing.max_file_size_mb * 1024 * 1024
    if features.size_bytes > max_bytes:
        failures.append(
            f"file_too_large: {features.size_bytes} bytes > cap {max_bytes}"
        )

    if features.pdf_encrypted_or_unreadable:
        failures.append("encrypted_or_unreadable_pdf")

    if not features.detected_mime:
        failures.append("mime_undetected")

    return ValidationResult(
        file_id=features.file_id,
        passed=not failures,
        failures=failures,
        detected_mime=features.detected_mime,
    )


def validate_text(
    file_id: str,
    text: str,
    config: AppConfig,
    guardrail: GuardrailClient,
    detected_mime: str | None = None,
) -> ValidationResult:
    """Stage B: validate extracted text - PII and language (spec 4.2).

    Runs BOTH the regex layer (cheap, format-obvious) and Guardrails (contextual),
    as the spec requires. Non-English handling is config-driven
    (``validation.non_english_action``, TODO(confirm) Q5): the default ``flag``
    records the language but still passes; ``reject`` fails validation.
    """
    failures: list[str] = []
    pii_findings: list[PiiFinding] = []

    # Regex PII (deterministic, offline).
    if config.validation.pii_regex_enabled:
        for f in regex_pii_findings(text):
            pii_findings.append(
                PiiFinding(kind=f.kind, detector="regex", masked_sample=f.masked_sample)
            )

    # Guardrails PII (contextual). In fake mode this reuses the regex layer.
    for g in guardrail.scan_pii(text):
        pii_findings.append(
            PiiFinding(kind=g.kind, detector="guardrail", masked_sample=g.masked_sample)
        )

    if pii_findings:
        failures.append(f"pii_detected: {len(pii_findings)} finding(s)")

    language = detect_language(text)
    if language != "en":
        action = config.validation.non_english_action
        if action == "reject":
            failures.append(f"non_english: {language}")
        # 'flag' and 'ignore' do not fail; 'flag' is recorded via the language field.

    return ValidationResult(
        file_id=file_id,
        passed=not failures,
        failures=failures,
        detected_mime=detected_mime,
        language=language,
        pii_findings=pii_findings,
    )


def detect_language(text: str, default: str = "en") -> str:
    """Detect the dominant language of ``text`` (ISO code), defaulting to en.

    Uses langdetect with a fixed seed so results are reproducible (spec 2.3).
    Very short text is treated as English rather than guessed wildly.
    """
    sample = text.strip()
    if len(sample) < 20:
        return default
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(sample)
    except Exception:
        return default


def needs_escalation(text: str, config: AppConfig) -> bool:
    """Deterministic escalation backstop after a DEFAULT parse (spec 4.2).

    Even when the rule table chose DEFAULT, escalate to BDA if the extracted text
    looks bad: too few chars per page, or too high a share of garbled /
    non-printable characters. This lets the rule table err toward cheap DEFAULT
    with a safety net.
    """
    esc = config.routing.escalation
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) < esc.min_chars_per_page_floor:
        return True
    garbled = sum(1 for c in stripped if not (c.isprintable() or c.isspace()))
    if garbled / len(stripped) > esc.max_garbled_char_ratio:
        return True
    return False
