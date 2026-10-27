"""The sample-platform adapter - the one adapter built this week (spec 7.1).

Maps the synthetic sample-pack ticket export (produced by
``sample_pack/generate_tickets.py``) onto the uniform Ticket schema. The sample
export already uses our canonical column names, but the adapter still goes
through the full mapping path (with alternative column-name candidates) so it
exercises the same code a real Zendesk/ServiceNow adapter would.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from kb_agent.dormant.tickets.adapters.base import TicketAdapter, parse_datetime
from kb_agent.models import Ticket


class SampleAdapter(TicketAdapter):
    """Adapter for the synthetic 'sample' platform."""

    source_platform = "sample"

    # Uniform field -> candidate raw columns in priority order. The alternative
    # names mimic common helpdesk exports so the mapping logic is real.
    field_map = {
        "ticket_id": ("ticket_id", "id", "number"),
        "subject": ("subject", "short_description", "title"),
        "description": ("description", "body", "details", "comment"),
        "resolution": ("resolution", "resolution_notes", "close_notes"),
        "status": ("status", "state"),
        "created_at": ("created_at", "created", "opened_at"),
        "resolved_at": ("resolved_at", "closed_at", "resolved"),
        "tags": ("tags", "labels"),
    }

    _STATUS_MAP = {
        "solved": "solved",
        "closed": "closed",
        "open": "open",
        "pending": "pending",
        "resolved": "solved",
        "new": "open",
    }

    def _map_row(self, row: dict[str, Any]) -> Ticket | None:
        ticket_id = self._first_present(row, "ticket_id")
        subject = self._first_present(row, "subject")
        description = self._first_present(row, "description")

        # Required fields: without an id and some text there is nothing to use.
        if not ticket_id or (not subject and not description):
            return None

        created = parse_datetime(self._first_present(row, "created_at"))
        if created is None:
            # created_at is required by the schema; default to epoch-less now only
            # if truly absent would hide data problems, so we skip instead.
            return None

        raw_status = str(self._first_present(row, "status") or "unknown").lower()
        status = self._STATUS_MAP.get(raw_status, "unknown")

        tags_raw = self._first_present(row, "tags")
        tags = _split_tags(tags_raw)

        resolution = self._first_present(row, "resolution")
        resolved_at = parse_datetime(self._first_present(row, "resolved_at"))

        return Ticket(
            ticket_id=str(ticket_id),
            subject=str(subject or ""),
            description=str(description or ""),
            resolution=str(resolution) if resolution else None,
            status=status,  # type: ignore[arg-type]  # validated against Literal
            created_at=created,
            resolved_at=resolved_at,
            tags=tags,
            source_platform=self.source_platform,
        )


def _split_tags(value: Any) -> list[str]:
    """Split a tags cell into a list. Accepts list, '|'- or ','-separated text."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    text = str(value)
    sep = "|" if "|" in text else ","
    return [t.strip() for t in text.split(sep) if t.strip()]


# Convenience for tests / fixtures: a trivially-constructed epoch.
EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
