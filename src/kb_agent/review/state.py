"""Artefact review state and decision records (spec 2.2).

"Everything is a draft until approved" is enforced in code, not prompts. Every
artefact carries a status, and every state transition records reviewer identity,
timestamp, free-text feedback, and the model + prompt version that produced the
artefact (spec 2.2). This module is the single place that holds that state and
validates transitions.

Where this state is durably stored (S3-only vs DynamoDB) is OPEN_QUESTIONS Q10;
this module keeps an in-memory registry with a ``to_records()`` export so the
storage backend is a later, isolated decision. The registry itself is backend-free.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from kb_agent.exceptions import KBAgentError
from kb_agent.models import ReviewStatus

# Allowed transitions. pending_review can go anywhere; terminal states are fixed.
_ALLOWED: dict[ReviewStatus, set[ReviewStatus]] = {
    ReviewStatus.PENDING_REVIEW: {
        ReviewStatus.APPROVED,
        ReviewStatus.REJECTED,
        ReviewStatus.RERUN_REQUESTED,
    },
    ReviewStatus.RERUN_REQUESTED: {ReviewStatus.PENDING_REVIEW},
    ReviewStatus.APPROVED: set(),
    ReviewStatus.REJECTED: set(),
}


class DecisionRecord(BaseModel):
    """One recorded review decision / state transition (spec 2.2)."""

    # 'model_id' is the audit term; silence pydantic's protected 'model_' guard.
    model_config = ConfigDict(protected_namespaces=())

    artifact_id: str
    from_status: ReviewStatus
    to_status: ReviewStatus
    reviewer: str
    comment: str = ""
    model_id: str | None = None
    prompt_version: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactState(BaseModel):
    """Current review state of one artefact plus its provenance + history."""

    model_config = ConfigDict(protected_namespaces=())

    artifact_id: str
    kind: str  # e.g. "article", "intent_view", "report"
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    model_id: str | None = None
    prompt_version: str | None = None
    uri: str | None = None
    history: list[DecisionRecord] = Field(default_factory=list)


class ReviewRegistry:
    """In-memory registry of artefact states for a run (spec 2.2).

    Backend-free by design (Q10): ``to_records`` exports a JSON-serialisable
    snapshot that the pipeline writes to S3 (or later a state table).
    """

    def __init__(self) -> None:
        self._states: dict[str, ArtifactState] = {}

    def register(
        self,
        artifact_id: str,
        kind: str,
        *,
        model_id: str | None = None,
        prompt_version: str | None = None,
        uri: str | None = None,
    ) -> ArtifactState:
        """Register a new artefact as pending_review."""
        state = ArtifactState(
            artifact_id=artifact_id,
            kind=kind,
            model_id=model_id,
            prompt_version=prompt_version,
            uri=uri,
        )
        self._states[artifact_id] = state
        return state

    def get(self, artifact_id: str) -> ArtifactState:
        return self._states[artifact_id]

    def all(self) -> list[ArtifactState]:
        return list(self._states.values())

    def pending(self) -> list[ArtifactState]:
        return [s for s in self._states.values() if s.status == ReviewStatus.PENDING_REVIEW]

    def transition(
        self,
        artifact_id: str,
        to_status: ReviewStatus,
        *,
        reviewer: str,
        comment: str = "",
    ) -> ArtifactState:
        """Apply a reviewer decision, validating the transition (spec 2.2).

        Records reviewer identity, timestamp, comment, and the artefact's model +
        prompt version. Rejects illegal transitions (e.g. approving twice).
        """
        state = self._states[artifact_id]
        allowed = _ALLOWED.get(state.status, set())
        if to_status not in allowed:
            raise KBAgentError(
                f"illegal transition for {artifact_id}: {state.status.value} -> {to_status.value}"
            )
        record = DecisionRecord(
            artifact_id=artifact_id,
            from_status=state.status,
            to_status=to_status,
            reviewer=reviewer,
            comment=comment,
            model_id=state.model_id,
            prompt_version=state.prompt_version,
        )
        state.status = to_status
        state.history.append(record)
        return state

    def to_records(self) -> list[dict]:
        """JSON-serialisable snapshot for the run manifest / durable store."""
        return [s.model_dump(mode="json") for s in self._states.values()]
