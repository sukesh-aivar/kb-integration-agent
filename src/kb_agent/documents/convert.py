"""Deterministic structured-document conversion to Markdown (Part B, B2.6).

The Bedrock Knowledge Base parser handles PDF/DOCX/text natively but reads
structured formats poorly or not at all: JSON is unsupported, CSV is parsed one
row at a time against a single content field, and XLSX behaviour is shallow. So
for CSV, JSON/JSONL, and XLSX/XLSM we extract the content ourselves into clean,
self-describing Markdown and let the KB parser ingest that instead (route
``CONVERT``). Extraction is the only stored transformation outside BDA.

Design rules (all deterministic, byte-stable on repeated runs):
* JSON/JSONL via stdlib ``json``; CSV via stdlib ``csv``; XLSX/XLSM via
  ``openpyxl`` (no pandas). Legacy ``.xls`` is NOT supported (``xlrd`` is
  deliberately not a dependency) and raises :class:`UnsupportedFormatError`.
* Never put raw content in an exception message. A parse error reports the
  location (line/column, or sheet) only.
* Output larger than the KB per-file limit is split at record/row boundaries into
  ``name.part-001.md``, ``name.part-002.md`` ... each under the limit.

The module is pure: it takes bytes and returns :class:`ConversionResult`; callers
(the normaliser) decide the destination keys and write them.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from pydantic import BaseModel, Field


class UnsupportedFormatError(ValueError):
    """Raised for a structured format we deliberately do not convert (e.g. .xls)."""


class ConversionError(ValueError):
    """Raised when a structured file cannot be parsed. Message carries LOCATION only."""


class ConversionPart(BaseModel):
    """One output fragment: a suffix (``""`` or ``part-001``) and its Markdown."""

    suffix: str
    markdown: str


class ConversionResult(BaseModel):
    """The Markdown extraction of one structured file, possibly split into parts."""

    source_format: str
    parts: list[ConversionPart] = Field(default_factory=list)

    @property
    def part_count(self) -> int:
        return len(self.parts)


# --- public entry point ------------------------------------------------------


def convert(
    name: str, fmt: str, data: bytes, *, max_part_bytes: int, max_depth: int
) -> ConversionResult:
    """Convert a structured file's bytes to Markdown part(s).

    Args:
        name: original file name (used only for section titles / error locations).
        fmt: short format from ``detect_format`` (json/jsonl/csv/xlsx/xlsm/xls).
        data: the full file bytes.
        max_part_bytes: KB per-file limit in bytes; output is split below this.
        max_depth: nested-object flattening depth cap (JSON).

    Returns:
        A :class:`ConversionResult` with one or more parts.

    Raises:
        UnsupportedFormatError: for ``xls``.
        ConversionError: on a parse failure (message carries location only).
    """
    if fmt in ("json", "jsonl"):
        blocks = _json_blocks(name, fmt, data, max_depth=max_depth)
    elif fmt == "csv":
        blocks = _csv_blocks(name, data)
    elif fmt in ("xlsx", "xlsm"):
        blocks = _xlsx_blocks(name, data)
    elif fmt == "xls":
        raise UnsupportedFormatError(
            "legacy .xls is not supported; re-save the workbook as .xlsx"
        )
    else:  # pragma: no cover - router never routes other formats to CONVERT
        raise UnsupportedFormatError(f"cannot convert format {fmt!r}")

    parts = _pack_blocks(blocks, max_part_bytes=max_part_bytes)
    return ConversionResult(source_format=fmt, parts=parts)


# --- block packing (record-boundary splitting) -------------------------------


def _pack_blocks(blocks: list[str], *, max_part_bytes: int) -> list[ConversionPart]:
    """Join self-contained blocks into parts, each under ``max_part_bytes``.

    Splitting happens only at block (record/row/sheet) boundaries so a record is
    never cut in half. A single block larger than the limit is emitted on its own
    (it cannot be split without breaking the record; the caller will see it and
    the KB may still reject it - that is visible, not silent).
    """
    if not blocks:
        return [ConversionPart(suffix="", markdown="")]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    sep = "\n\n"
    sep_len = len(sep.encode("utf-8"))

    for block in blocks:
        block_len = len(block.encode("utf-8"))
        if current and current_len + sep_len + block_len > max_part_bytes:
            parts.append(sep.join(current))
            current = [block]
            current_len = block_len
        else:
            current_len += (sep_len + block_len) if current else block_len
            current.append(block)
    if current:
        parts.append(sep.join(current))

    if len(parts) == 1:
        return [ConversionPart(suffix="", markdown=parts[0])]
    return [
        ConversionPart(suffix=f"part-{i + 1:03d}", markdown=text)
        for i, text in enumerate(parts)
    ]


# --- JSON / JSONL ------------------------------------------------------------


def _json_blocks(name: str, fmt: str, data: bytes, *, max_depth: int) -> list[str]:
    """Render JSON/JSONL to a list of self-contained Markdown blocks."""
    text = _decode_utf8(data)
    if fmt == "jsonl":
        return _jsonl_blocks(text, max_depth=max_depth)

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConversionError(
            f"invalid JSON in {name} at line {exc.lineno}, column {exc.colno}"
        ) from None

    if isinstance(doc, list):
        blocks = []
        for i, element in enumerate(doc, start=1):
            blocks.append(_record_block(f"Record {i}", element, max_depth=max_depth))
        return blocks or [f"## {name}\n\n(empty array)"]
    if isinstance(doc, dict):
        return [_record_block(name, doc, max_depth=max_depth)]
    # A bare scalar / top-level non-container.
    return [f"## {name}\n\n{_scalar(doc)}"]


def _jsonl_blocks(text: str, *, max_depth: int) -> list[str]:
    blocks = []
    n = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConversionError(
                f"invalid JSON on line {lineno}, column {exc.colno}"
            ) from None
        n += 1
        blocks.append(_record_block(f"Record {n}", obj, max_depth=max_depth))
    return blocks or ["(empty JSONL file)"]


def _record_block(title: str, obj: Any, *, max_depth: int) -> str:
    """One ``## {title}`` block: a ``- **key**: value`` line per (flattened) field."""
    lines = [f"## {title}", ""]
    if isinstance(obj, dict):
        for key, value in _flatten(obj, max_depth=max_depth):
            lines.append(f"- **{key}**: {value}")
    elif isinstance(obj, list):
        lines.append(_render_array(obj, max_depth=max_depth))
    else:
        lines.append(_scalar(obj))
    return "\n".join(lines)


def _flatten(
    obj: dict, *, max_depth: int, prefix: str = "", depth: int = 1
) -> list[tuple[str, str]]:
    """Flatten nested objects to dotted keys, capping at ``max_depth``.

    Beyond the depth cap a nested container is rendered as compact JSON on one
    line (deterministic via ``sort_keys``) rather than exploded further.
    """
    out: list[tuple[str, str]] = []
    for key in obj:  # preserve input order (dict keeps insertion order)
        value = obj[key]
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            if depth >= max_depth:
                out.append((dotted, _compact(value)))
            else:
                out.extend(
                    _flatten(value, max_depth=max_depth, prefix=f"{dotted}.", depth=depth + 1)
                )
        elif isinstance(value, list):
            out.append((dotted, _render_array(value, max_depth=max_depth, depth=depth)))
        else:
            out.append((dotted, _scalar(value)))
    return out


def _render_array(arr: list, *, max_depth: int, depth: int = 1) -> str:
    """Arrays of scalars join with '; '; arrays of objects become numbered sub-blocks."""
    if not arr:
        return "(empty list)"
    if all(not isinstance(x, dict | list) for x in arr):
        return "; ".join(_scalar(x) for x in arr)
    # Array of objects/arrays: numbered inline sub-records.
    sub = []
    for i, element in enumerate(arr, start=1):
        if isinstance(element, dict):
            if depth >= max_depth:
                sub.append(f"{i}. {_compact(element)}")
            else:
                fields = "; ".join(
                    f"{k}: {v}" for k, v in _flatten(element, max_depth=max_depth, depth=depth + 1)
                )
                sub.append(f"{i}. {fields}")
        else:
            sub.append(f"{i}. {_scalar(element)}")
    return "\n  " + "\n  ".join(sub)


def _compact(value: Any) -> str:
    """Deterministic one-line JSON for a value beyond the flatten depth cap."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _scalar(value: Any) -> str:
    """Render a JSON scalar: numbers/bools/null as their literals, strings escaped."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int | float):
        return str(value)
    return _escape_md(str(value))


# --- CSV ---------------------------------------------------------------------


def _csv_blocks(name: str, data: bytes) -> list[str]:
    """Render a CSV to a single Markdown table block (header row = first row)."""
    text = _decode_utf8(data)
    try:
        reader = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ConversionError(f"invalid CSV in {name}: {type(exc).__name__}") from None
    if not reader:
        return [f"## {name}\n\n(empty file)"]

    header = reader[0]
    rows = reader[1:]
    width = len(header)
    lines = [f"## {name}", "", _md_row([_escape_md(c) for c in header]), _md_divider(width)]
    for row in rows:
        # pad/truncate so the table stays rectangular and deterministic
        cells = (row + [""] * width)[:width]
        lines.append(_md_row([_escape_md(c) for c in cells]))
    return ["\n".join(lines)]


# --- XLSX / XLSM -------------------------------------------------------------


def _xlsx_blocks(name: str, data: bytes) -> list[str]:
    """Render each worksheet to its own Markdown table block (one block per sheet).

    Merged cells are forward-filled so every spanned cell carries the anchor
    value. Formulas are rendered as their cached values (``data_only=True``);
    a workbook that was never opened in a spreadsheet app may have no cached
    values, in which case the formula text is shown.
    """
    try:
        import openpyxl
    except Exception:  # pragma: no cover
        raise ConversionError(f"cannot read {name}: openpyxl unavailable") from None

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    except Exception as exc:
        raise ConversionError(f"cannot open workbook {name}: {type(exc).__name__}") from None

    blocks: list[str] = []
    try:
        for ws in wb.worksheets:
            blocks.append(_sheet_block(ws))
    finally:
        wb.close()
    return blocks or [f"## {name}\n\n(no sheets)"]


def _sheet_block(ws: Any) -> str:
    """One ``## Sheet: {title}`` block with a Markdown table."""
    title = f"## Sheet: {_escape_md(str(ws.title))}"
    grid = _materialise_sheet(ws)
    if not grid:
        return f"{title}\n\n(empty sheet)"

    width = max(len(r) for r in grid)
    grid = [(r + [""] * width)[:width] for r in grid]
    header = grid[0]
    lines = [title, "", _md_row(header), _md_divider(width)]
    for row in grid[1:]:
        lines.append(_md_row(row))
    return "\n".join(lines)


def _materialise_sheet(ws: Any) -> list[list[str]]:
    """Return the sheet as a rectangular list-of-lists of strings, merges filled."""
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append(["" if v is None else _cell_str(v) for v in row])

    # Forward-fill merged ranges with the top-left (anchor) value.
    for merged_range in ws.merged_cells.ranges:
        min_row, min_col = merged_range.min_row, merged_range.min_col
        max_row, max_col = merged_range.max_row, merged_range.max_col
        if min_row - 1 >= len(rows) or min_col - 1 >= len(rows[min_row - 1]):
            continue
        anchor = rows[min_row - 1][min_col - 1]
        for r in range(min_row, max_row + 1):
            if r - 1 >= len(rows):
                continue
            for c in range(min_col, max_col + 1):
                if c - 1 < len(rows[r - 1]):
                    rows[r - 1][c - 1] = anchor

    # Trim fully-empty trailing rows for determinism.
    while rows and all(cell == "" for cell in rows[-1]):
        rows.pop()
    return rows


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return _escape_md(str(value))


# --- shared helpers ----------------------------------------------------------


def _decode_utf8(data: bytes) -> str:
    """UTF-8 decode, tolerant of a BOM. Raises ConversionError on invalid bytes."""
    try:
        if data[:3] == b"\xef\xbb\xbf":
            data = data[3:]
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConversionError(
            f"file is not valid UTF-8 (byte {exc.start})"
        ) from None


def _escape_md(text: str) -> str:
    """Escape only what would break Markdown table / structure, keep it readable."""
    # Collapse newlines (they would break a table row) and escape pipes.
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _md_divider(width: int) -> str:
    return "| " + " | ".join(["---"] * max(width, 1)) + " |"
