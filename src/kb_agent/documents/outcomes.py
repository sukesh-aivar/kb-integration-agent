"""Per-file outcome records and run-level aggregation (Part B, B3.5).

Handling many files in one run means the answer is per-file, not per-run: one bad
file never fails the whole run. Each discovered object gets a :class:`FileOutcome`
recording what format it was, how it was routed, and what became of it. The
normaliser fills these in; ``kb_ingest`` later upgrades ``staged`` entries to
``ingested`` or ``failed``.

Rejection/skip reasons are plain English and NEVER contain file content (spec:
no document or row text in any state file or event).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Per-file status vocabulary (B3.5).
#   staged            - written to the KB prefix, awaiting the ingestion job
#   ingested          - the ingestion job indexed it (set in kb_ingest)
#   rejected          - unsupported / over-limit / unreadable (a clear reason)
#   failed            - an error processing this one file (run continues)
#   skipped_duplicate - identical content to an earlier file in this run
#   unchanged         - already present with the same content hash (skip write)
#   ignored           - bookkeeping noise (dotfile, __MACOSX, README, ...)
FileStatus = Literal[
    "staged", "ingested", "rejected", "failed", "skipped_duplicate", "unchanged", "ignored",
]


class FileOutcome(BaseModel):
    """The outcome of processing one source object (B3.5)."""

    name: str  # the original name / relative path (never dropped)
    source_uri: str
    size: int = 0
    format: str = "unknown"
    route: str | None = None  # default | bda | convert | reject (None if ignored)
    rule_id: int | None = None
    reason: str = ""
    pages: int | None = None
    content_hash: str | None = None
    status: FileStatus = "staged"
    status_reason: str | None = None
    kb_key: str | None = None  # destination key under the project kb-docs prefix
    extraction_uri: str | None = None  # per-run copy of a BDA/convert extraction
    bda_cached: bool | None = None
    part_count: int | None = None  # for CONVERT outputs split into parts


class NormalizationResult(BaseModel):
    """Everything the normaliser produced for one run (B3)."""

    outcomes: list[FileOutcome] = Field(default_factory=list)

    @property
    def staged(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status == "staged"]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def aggregate_run_status(outcomes: list[FileOutcome]) -> tuple[str, list[str]]:
    """Decide the run-level outcome from the per-file outcomes (B3.5).

    Returns ``(disposition, warnings)`` where disposition is one of:

    * ``"no_documents_ingested"`` - every file ended rejected/failed/ignored (the
      run should fail with this error code).
    * ``"ok"`` - at least one file is staged/ingested; the run is ``completed``.
      ``warnings`` lists any rejected/failed files (a deliberate partial-success
      choice; the consumer decides what a partial result means).
    """
    productive = [o for o in outcomes if o.status in ("staged", "ingested", "unchanged")]
    warnings: list[str] = []
    for o in outcomes:
        if o.status in ("rejected", "failed"):
            warnings.append(f"{o.name}: {o.status} - {o.status_reason or o.reason}")

    if not productive:
        return "no_documents_ingested", warnings
    return "ok", warnings
