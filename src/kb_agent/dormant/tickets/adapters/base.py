"""The TicketAdapter interface (spec 4.3 adapter).

Different helpdesk platforms name fields differently (``short_description`` vs
``subject``). An adapter is a DECLARATIVE per-platform mapping to the one uniform
internal schema (:class:`~kb_agent.models.Ticket`), behind this common interface
so a new platform is one new mapping subclass, not a pipeline change.

This mapping is deliberately deterministic (spec 2.1): an LLM per record is
costly, non-deterministic, and can place a value in the wrong field while still
passing schema validation. An LLM may *propose* a mapping for an unknown format
once, for a human to approve - but it never runs per record. That proposal path
is not built this pass.

Ground-truth safety (spec 2.4): the adapter NEVER reads a seeded-intent field
into the Ticket. Labels live only in the separate ``labels.csv`` and are joined
by ``ticket_id`` for scoring, outside the pipeline.
"""

from __future__ import annotations

import datetime as _dt
from abc import ABC, abstractmethod
from typing import Any

from kb_agent.logging_setup import get_logger
from kb_agent.models import MappingReport, Ticket

logger = get_logger(__name__)

# Fields the adapter must never map from, even if present in the raw export.
# This is a hard guard against ground-truth leakage (spec 2.4).
FORBIDDEN_SOURCE_FIELDS = frozenset(
    {"seeded_intent", "_seeded_intent", "intent_label", "gold_intent", "edge_case"}
)


class TicketAdapter(ABC):
    """Base class for a per-platform mapping to the uniform Ticket schema."""

    #: Platform name recorded on every ticket (e.g. "sample", "zendesk").
    source_platform: str = "unknown"

    #: Mapping of uniform field name -> candidate raw column names, in priority
    #: order. Subclasses override. The FIRST present column wins.
    field_map: dict[str, tuple[str, ...]] = {}

    def adapt(self, rows: list[dict[str, Any]]) -> tuple[list[Ticket], MappingReport]:
        """Map raw rows to Tickets, collecting a mapping report.

        A single bad row never fails the run (spec 7.4): it is counted as skipped
        or failed with a reason, and processing continues.
        """
        report = MappingReport(source_platform=self.source_platform)
        tickets: list[Ticket] = []

        for i, row in enumerate(rows):
            self._assert_no_forbidden_fields(row)
            try:
                ticket = self._map_row(row)
            except Exception as exc:  # noqa: BLE001 - want to continue past any bad row
                report.failed += 1
                report.reasons.append(f"row {i}: {type(exc).__name__}: {exc}")
                continue
            if ticket is None:
                report.skipped += 1
                report.reasons.append(f"row {i}: skipped (missing required fields)")
                continue
            tickets.append(ticket)
            report.mapped += 1

        logger.info(
            "adapt_complete",
            platform=self.source_platform,
            mapped=report.mapped,
            skipped=report.skipped,
            failed=report.failed,
        )
        return tickets, report

    def _assert_no_forbidden_fields(self, row: dict[str, Any]) -> None:
        """Guard: refuse to even see a ground-truth column (spec 2.4).

        If a raw export somehow carries a seeded-label column, we do not map it;
        we log that it was present and ignored. We never copy it onto a Ticket.
        """
        present = FORBIDDEN_SOURCE_FIELDS.intersection(row.keys())
        if present:
            logger.warning("forbidden_fields_ignored", fields=sorted(present))

    def _first_present(self, row: dict[str, Any], field: str) -> Any:
        """Return the first mapped raw value for a uniform field, or None."""
        for candidate in self.field_map.get(field, ()):  # priority order
            if candidate in FORBIDDEN_SOURCE_FIELDS:
                continue
            if candidate in row and row[candidate] not in (None, ""):
                return row[candidate]
        return None

    @abstractmethod
    def _map_row(self, row: dict[str, Any]) -> Ticket | None:
        """Map one raw row to a Ticket, or None to skip it. Subclass implements."""
        raise NotImplementedError


def parse_datetime(value: Any) -> _dt.datetime | None:
    """Best-effort ISO/epoch datetime parse used by adapters.

    Returns None on anything unparseable rather than raising, so one bad
    timestamp does not fail a row outright (the caller decides).
    """
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)
    text = str(value).strip()
    try:
        dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=_dt.UTC)
    except ValueError:
        pass
    try:
        return _dt.datetime.fromtimestamp(float(text), tz=_dt.UTC)
    except (ValueError, OSError):
        return None
