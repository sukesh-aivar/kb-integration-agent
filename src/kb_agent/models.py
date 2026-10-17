"""Core domain models (spec 4.3 ticket_profile, 4.2 documents, 4.3/4.4 outputs).

These are the typed objects that flow between pipeline stages. Keeping them in
one module gives every stage a shared, validated vocabulary.

Ground-truth safety (spec 2.4): the seeded intent label is NEVER a field on the
object that reaches a prompt or an embedding. :class:`Ticket` carries it as a
private attribute only for convenience in test fixtures; the canonical, safe
design keeps labels in a separate ``labels.csv`` keyed by ``ticket_id``. A
dedicated test asserts no seeded label string ever appears in a prompt payload.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# --- Shared enums ------------------------------------------------------------


class ReviewStatus(str, Enum):
    """Lifecycle status carried by every reviewable artefact (spec 2.2)."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    RERUN_REQUESTED = "rerun_requested"


class Route(str, Enum):
    """Document routing outcomes (spec 4.2 rule table)."""

    DEFAULT = "DEFAULT"
    BDA = "BDA"
    REJECT = "REJECT"


class SourceType(str, Enum):
    """Provenance of KB content (spec 4.2 normalize_docs)."""

    UPLOADED_DOC = "uploaded_doc"
    TICKET_DERIVED = "ticket_derived"


class CoverageStatus(str, Enum):
    """Coverage-check verdict per intent (spec 4.4)."""

    COVERED = "covered"
    PARTIAL = "partial"
    GAP = "gap"


# --- Tickets (spec 4.3) ------------------------------------------------------


class Ticket(BaseModel):
    """One customer support case: the problem, and how a human resolved it.

    This is the uniform internal schema (``ticket_profile``) that every
    per-platform adapter maps onto. ``subject + description`` is the only text
    embedded for clustering - resolutions and metadata are deliberately excluded
    (spec 4.3 clean_tickets).
    """

    ticket_id: str
    subject: str
    description: str  # the customer's problem, as stated
    resolution: str | None = None  # what the human agent did; None = unresolved
    status: Literal["solved", "open", "pending", "closed", "unknown"] = "unknown"
    created_at: datetime
    resolved_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None  # filled by validation
    source_platform: str = "sample"

    # Ground truth, TEST DATA ONLY. MUST NOT reach any prompt or embedding.
    # Stored as a private attr so it is not serialised by model_dump() by
    # default and cannot be accidentally dropped into a prompt payload built
    # from the public model. Canonical storage remains a separate labels.csv.
    _seeded_intent: str | None = PrivateAttr(default=None)

    def embedding_text(self) -> str:
        """Return the text used for clustering embeddings.

        Only ``subject + description`` - intent is what the customer asked for,
        not how it was fixed (spec 4.3).
        """
        return f"{self.subject}\n\n{self.description}".strip()


class MappingReport(BaseModel):
    """Result of adapting a raw ticket export to the uniform schema (spec 4.3).

    A single bad row never fails the run; it is counted here with a reason.
    """

    mapped: int = 0
    skipped: int = 0
    failed: int = 0
    reasons: list[str] = Field(default_factory=list)
    source_platform: str = "sample"


# --- Documents (spec 4.2) ----------------------------------------------------


class PiiFinding(BaseModel):
    """A single PII detection (from Guardrails or the regex layer)."""

    kind: str  # e.g. "email", "phone", "card", "national_id"
    detector: Literal["guardrail", "regex"]
    masked_sample: str = ""  # masked excerpt only; never the raw value


class ValidationResult(BaseModel):
    """Output of validate_uploads (spec 4.2)."""

    file_id: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    detected_mime: str | None = None
    language: str | None = None
    pii_findings: list[PiiFinding] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    """Output of route_documents (spec 4.2).

    ``rule_id`` is what makes a misroute debuggable in the audit log.
    """

    file_id: str
    route: Route
    rule_id: int
    features: dict[str, float | int | bool | str] = Field(default_factory=dict)
    reason: str


class DocumentMetadata(BaseModel):
    """Metadata sidecar written next to each normalised document (spec 4.2).

    TODO(confirm) Q (sidecar format): exact file naming/format Bedrock KB expects
    is unverified; see OPEN_QUESTIONS.md.
    """

    source: str
    source_type: SourceType
    route: Route
    content_hash: str
    run_id: str
    ingested_at: datetime
    language: str | None = None


# --- Intents (spec 4.3) ------------------------------------------------------


class IntentCluster(BaseModel):
    """A named cluster of tickets representing one discovered intent (spec 4.3)."""

    cluster_id: int  # -1 is the HDBSCAN noise bucket
    intent_name: str
    description: str
    volume: int
    resolved_share: float
    example_ticket_ids: list[str] = Field(default_factory=list)
    coherence_flag: bool = False  # True = model thinks the cluster mixes intents
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW


# --- Articles (spec 4.4) -----------------------------------------------------


class Article(BaseModel):
    """A drafted knowledge-base article (spec 4.4 draft_article).

    Written only to ``drafts/{run_id}/`` - a prefix the KB data source cannot
    read (spec 2.2). Citations are mandatory and validated in code.
    """

    # 'model_id' would collide with pydantic's protected 'model_' namespace;
    # we keep the field name (it is the audit term) and silence the warning.
    model_config = ConfigDict(protected_namespaces=())

    article_id: str
    intent_name: str
    title: str
    problem: str
    applies_to: str
    steps: list[str]
    resolution: str
    source_ticket_ids: list[str]  # citations; each validated to exist + belong
    model_id: str
    prompt_version: str
    pii_scan_passed: bool
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW


# --- Coverage (spec 4.4) -----------------------------------------------------


class CoverageItem(BaseModel):
    """Coverage result for one intent (spec 4.4 coverage_check)."""

    intent_name: str
    status: CoverageStatus
    volume: int
    evidence_passages: list[str] = Field(default_factory=list)
    priority: float = 0.0  # volume x gap
    reason: str = ""
