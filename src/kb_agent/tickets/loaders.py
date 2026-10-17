"""load_tickets: read a raw ticket export into row dicts (spec 4.3).

A ticket is structured conversational data, not a document, so it never goes near
BDA. This module only reads bytes/paths into a list of raw row dicts - it does NOT
map fields to our schema (that is the adapter's job, kept separate so a new
platform is one new mapping, not a loader change). Format is TODO(confirm) Q2;
CSV and JSON are first-class, XLSX via the same return shape.

Loaders return raw rows with their original column names untouched, so the
mapping report (adapter) can faithfully record what was and was not mapped.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from kb_agent.exceptions import ValidationError


def load_tickets_bytes(data: bytes, fmt: str) -> list[dict[str, Any]]:
    """Load raw ticket rows from in-memory bytes.

    Args:
        data: Raw file bytes.
        fmt: One of ``"csv"``, ``"json"``, ``"xlsx"``.

    Returns:
        A list of row dicts with original column names.

    Raises:
        ValidationError: If the format is unsupported or the payload is malformed.
    """
    fmt = fmt.lower()
    if fmt == "csv":
        return _load_csv(data)
    if fmt == "json":
        return _load_json(data)
    if fmt == "xlsx":
        return _load_xlsx(data)
    raise ValidationError(f"Unsupported ticket format: {fmt!r}")


def load_tickets_uri(uri: str, store: Any, fmt: str | None = None) -> list[dict[str, Any]]:
    """Load raw ticket rows from an object-store key/URI.

    The format is inferred from the extension if not given. ``store`` is any
    :class:`ObjectStore`; the key is the URI with any ``s3://bucket/`` prefix
    stripped.
    """
    key = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri
    inferred = fmt or key.rsplit(".", 1)[-1].lower()
    return load_tickets_bytes(store.get_bytes(key), inferred)


def _load_csv(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")  # tolerate a BOM
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _load_json(data: bytes) -> list[dict[str, Any]]:
    payload = json.loads(data.decode("utf-8"))
    # Accept either a bare list or {"tickets": [...]} / {"records": [...]}.
    if isinstance(payload, dict):
        for key in ("tickets", "records", "data"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        raise ValidationError("JSON object has no 'tickets'/'records'/'data' list")
    if isinstance(payload, list):
        return payload
    raise ValidationError(f"Unexpected JSON top-level type: {type(payload).__name__}")


def _load_xlsx(data: bytes) -> list[dict[str, Any]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(h) if h is not None else "" for h in next(rows)]
    except StopIteration:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({header[i]: row[i] for i in range(min(len(header), len(row)))})
    wb.close()
    return out
