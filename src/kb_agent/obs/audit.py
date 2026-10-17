"""The run manifest / audit record (spec 5.2).

The SOW requires "inputs, prompts, model versions, tool calls, outputs and
decisions for every run, with export". This module builds a single JSON manifest
per run that accumulates those facts as stages run, then is written to S3 keyed by
``run_id``.

Deliberately records IDs, hashes, counts, model/prompt versions and durations -
NOT raw customer content (spec 5.2, 7.4). Model calls record the model id, prompt
name+version and (where available) token/latency, never the raw ticket body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class ModelCallRecord(BaseModel):
    """One model invocation, by metadata only (no raw content; spec 5.2)."""

    model_config = {"protected_namespaces": ()}

    model_id: str
    prompt_name: str
    prompt_version: str
    count: int = 1


class StageRecord(BaseModel):
    """Per-stage status, duration and output location (spec 5.2)."""

    stage: str
    status: str
    duration_sec: float | None = None
    output_location: str | None = None


class InputFileRecord(BaseModel):
    """One input file by metadata only (spec 5.2)."""

    name: str
    content_hash: str
    size_bytes: int
    detected_mime: str | None = None
    validation_passed: bool | None = None


class RunManifest(BaseModel):
    """The single per-run audit manifest (spec 5.2)."""

    # 'model_calls' trips pydantic's protected 'model_' namespace; it is an audit
    # term we keep deliberately.
    model_config = {"protected_namespaces": ()}

    run_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    library_versions: dict[str, str] = Field(default_factory=dict)
    input_files: list[InputFileRecord] = Field(default_factory=list)
    routing_decisions: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: list[ModelCallRecord] = Field(default_factory=list)
    stages: list[StageRecord] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    review_records: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    def record_stage(
        self, stage: str, status: str, duration_sec: float | None = None, output: str | None = None
    ) -> None:
        self.stages.append(
            StageRecord(
                stage=stage,
                status=status,
                duration_sec=duration_sec,
                output_location=output,
            )
        )

    def record_model_call(self, model_id: str, prompt_name: str, prompt_version: str) -> None:
        self.model_calls.append(
            ModelCallRecord(
                model_id=model_id, prompt_name=prompt_name, prompt_version=prompt_version
            )
        )

    def finish(self) -> None:
        self.ended_at = datetime.now(UTC)


def library_versions() -> dict[str, str]:
    """Snapshot key library versions for reproducibility (spec 2.3)."""
    versions: dict[str, str] = {}
    for name in ("boto3", "pydantic", "numpy", "umap", "hdbscan", "sklearn"):
        try:
            mod = __import__(name)
            versions[name] = getattr(mod, "__version__", "unknown")
        except Exception:  # pragma: no cover - depends on environment
            versions[name] = "not-installed"
    return versions
