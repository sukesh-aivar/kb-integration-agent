"""The agent contract: the orchestrator <-> agent integration surface (spec 8).

The SOW does not specify this interface, so we define it here and treat it as the
stable boundary - everything else can change behind it. Inputs arrive as S3
references, never payloads (spec 2.5): file contents must not travel the chat
path, and the agent's IAM role can then be scoped to the run's prefix.

TODO(confirm) Q9: synchronous vs polling, and how reviewer decisions are
delivered back. Until confirmed we support both a synchronous ``run()`` and a
stage-wise ``step()`` (see pipeline), and ``resume_from`` here enables the latter.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Options(BaseModel):
    """Per-run options the orchestrator may override (spec 8)."""

    kb_required: bool = True
    vector_store: str | None = None  # overrides config knowledge_base.vector_store
    threshold_overrides: dict[str, float] = Field(default_factory=dict)


class ReviewerFeedback(BaseModel):
    """Free-text reviewer feedback that can drive a re-run (spec 2.2, 6).

    Interpreting this to decide which stage to re-run is planner work (spec 6)
    and is out of scope for this build pass (OPEN_QUESTIONS Q12).
    """

    artifact_id: str
    decision: Literal["approve", "reject", "rerun"]
    reviewer: str
    comment: str = ""


class ArtifactRef(BaseModel):
    """A pointer to a reviewable artefact awaiting a decision."""

    name: str
    uri: str
    status: str


class ErrorRecord(BaseModel):
    """A non-fatal error collected during a run (spec 7.4: flag, continue)."""

    stage: str
    kind: str
    message: str
    file_id: str | None = None


class RunMetrics(BaseModel):
    """Headline metrics surfaced in the agent output (spec 1.5, 8).

    Fields are optional because a partial / resumed run may not have computed all
    of them yet.
    """

    recall_at_5: float | None = None
    intent_purity: float | None = None
    silhouette: float | None = None
    cluster_count: int | None = None
    noise_share: float | None = None
    pii_files_rejected: int | None = None
    gaps_flagged: int | None = None
    coverage_fp_rate: float | None = None
    routing_accuracy: float | None = None
    ticket_count: int | None = None
    document_count: int | None = None
    stage_durations_sec: dict[str, float] = Field(default_factory=dict)
    estimated_cost_usd: float | None = None


class AgentInput(BaseModel):
    """Invocation payload from the orchestrator (spec 8).

    S3 references only - never file payloads (spec 2.5).
    """

    run_id: str
    document_uris: list[str] = Field(default_factory=list)  # s3://.../uploads/{run_id}/docs/...
    ticket_uri: str | None = None  # s3://.../uploads/{run_id}/tickets.csv
    industry: str | None = None
    options: Options = Field(default_factory=Options)
    reviewer_feedback: ReviewerFeedback | None = None  # drives a re-run
    resume_from: str | None = None  # stage name, for resumable execution
    # --- console-deploy v2 additions (both optional; CLI/tests unaffected) ---
    # Opaque owner label supplied by the backend (Cognito sub). Written into
    # _started.json, manifest.json and the metadata sidecars beside run_id so the
    # data carries its owner. The agent never derives or accepts it elsewhere.
    owner_id: str | None = None
    # Hard cap on pages routed to Bedrock Data Automation, enforced BEFORE any
    # paid BDA call (spec 5.8). None = no cap (CLI default).
    max_pages: int | None = None


class AgentOutput(BaseModel):
    """Response returned to the orchestrator (spec 8)."""

    run_id: str
    status: Literal["completed", "running", "needs_review", "failed"]
    stage: str
    artifacts: dict[str, str] = Field(default_factory=dict)  # name -> s3 uri
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    pending_review: list[ArtifactRef] = Field(default_factory=list)
    errors: list[ErrorRecord] = Field(default_factory=list)
