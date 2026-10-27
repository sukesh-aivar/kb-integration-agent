"""Pure document feature extraction (spec 4.2, 7.4 "pure functions for logic").

This module measures a document and returns facts. It makes NO routing decision
and performs NO I/O decision - that separation is deliberate so the router
(router.py) is a pure function of these features and so each feature is testable
in isolation.

PDF geometry uses pdfplumber (MIT, on top of pdfminer.six) behind a small surface:
per-page char counts, image-area ratio, a multi-column heuristic, table detection
and encryption detection all come from one reader. PyMuPDF (AGPL) was dropped in
Part B (OPEN_QUESTIONS Q13) - pdfplumber was already a dependency, so this removes
the AGPL package without adding a new one. DOCX inspection uses python-docx. True
MIME comes from python-magic (bytes, never the extension).
"""

from __future__ import annotations

import io
from typing import Any

from pydantic import BaseModel, Field


class DocumentFeatures(BaseModel):
    """Locally-measured signals the router consumes. One per file.

    ``format`` is the short, routing-facing format name from :func:`detect_format`
    (e.g. ``pdf``, ``csv``, ``xlsx``, ``json``, ``unknown``). ``size_bytes`` and
    ``format`` are *cheap* (header + extension only); the PDF-geometry fields are
    *expensive* (they require opening the whole file) and are populated lazily -
    only when a rule that needs them is actually evaluated for a PDF that already
    passed the cheap size/format gates (B2.3).
    """

    file_id: str
    detected_mime: str
    size_bytes: int
    format: str = "unknown"
    # PDF-specific (0/empty/None until geometry is computed):
    pdf_encrypted_or_unreadable: bool = False
    min_page_chars: int | None = None  # fewest extracted chars on any page
    max_page_image_area_ratio: float = 0.0
    detected_tables: int = 0
    multi_column: bool = False
    page_count: int = 0
    # DOCX-specific:
    embedded_images: int = 0
    complex_tables: int = 0
    # True once the expensive PDF/DOCX geometry pass has run for this file.
    geometry_computed: bool = False
    extra: dict[str, str] = Field(default_factory=dict)


def detect_mime(data: bytes, *, fallback: str = "application/octet-stream") -> str:
    """Detect the true MIME type from bytes (never trust the extension; spec 4.2).

    A renamed file must be caught, so we read magic bytes. python-magic needs the
    libmagic system library; if it is unavailable we fall back rather than crash,
    and record that in the caller.
    """
    try:
        import magic

        mime = magic.from_buffer(data, mime=True)
        return mime or fallback
    except Exception:  # pragma: no cover - exercised only without libmagic
        return _sniff_mime_fallback(data, fallback)


def _sniff_mime_fallback(data: bytes, fallback: str) -> str:
    """Tiny magic-number sniffer used only if libmagic is missing."""
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04"):
        # zip-based: docx/xlsx; cannot distinguish cheaply -> generic office.
        return "application/zip"
    try:
        data.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return fallback


# Canonical short MIME names the router rules use.
MIME_ALIASES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


def normalise_mime(mime: str) -> str:
    """Map a full MIME type to the short name used in routing_rules.yaml."""
    return MIME_ALIASES.get(mime, mime)


# --- Format detection (B2.2) -------------------------------------------------

# The routing-facing format vocabulary. ``unknown`` is the catch-all the final
# REJECT rule matches on.
KNOWN_FORMATS = frozenset(
    {"pdf", "docx", "doc", "xlsx", "xlsm", "xls", "csv", "json", "jsonl",
     "txt", "md", "html", "jpeg", "png", "unknown"}
)

# Extension -> format. Used as the tie-break when libmagic reports a generic
# text/zip MIME (it is unreliable for CSV vs TXT, JSON vs TXT, and the
# zip-based office formats).
_EXT_FORMAT = {
    ".pdf": "pdf",
    ".docx": "docx", ".doc": "doc",
    ".xlsx": "xlsx", ".xlsm": "xlsm", ".xls": "xls",
    ".csv": "csv",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".txt": "txt", ".text": "txt",
    ".md": "md", ".markdown": "md",
    ".html": "html", ".htm": "html",
    ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png",
}

# MIME -> format for the cases libmagic gets right and the extension may not.
_MIME_FORMAT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/csv": "csv",
    "application/json": "json",
    "text/markdown": "md",
    "text/html": "html",
    "image/jpeg": "jpeg",
    "image/png": "png",
}

# libmagic reports these generically for the whole text family; trust the
# extension for the specific kind, because CSV/JSON/TXT are all "text/plain".
_AMBIGUOUS_TEXT_MIMES = frozenset(
    {"text/plain", "application/octet-stream", "application/csv",
     "application/vnd.ms-excel"}  # some libmagic builds label a CSV as ms-excel
)
# Zip-based office formats share the same magic bytes (PK\x03\x04); the
# extension disambiguates docx vs xlsx vs a plain zip.
_ZIP_MIMES = frozenset(
    {"application/zip",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
)


def _ext_of(name: str) -> str:
    """Lowercased file extension including the dot (``""`` if none)."""
    base = name.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def detect_format(name: str, head_bytes: bytes) -> str:
    """Return the short routing format for a file (B2.2).

    Strategy: trust libmagic's MIME first, but fall back to the extension for the
    text family (CSV/JSON/TXT all look like ``text/plain``) and the zip-based
    office formats (DOCX/XLSX share magic bytes). ``name`` supplies the
    extension tie-break; ``head_bytes`` is the file's leading bytes (a few KB is
    plenty - we never need the whole file to name it).

    Pure and cheap: no full download, no PDF parse.
    """
    ext = _ext_of(name)
    ext_fmt = _EXT_FORMAT.get(ext)
    mime = detect_mime(head_bytes)

    # 0) Definitive magic signatures win outright (robust even when libmagic on a
    # short/truncated head returns text/plain).
    if head_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head_bytes[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head_bytes[:5] == b"%PDF-" or head_bytes[:4] == b"%PDF":
        return "pdf"

    # 1) Zip-based office: magic can't tell docx from xlsx; the extension does.
    if mime in _ZIP_MIMES or head_bytes[:4] == b"PK\x03\x04":
        if ext_fmt in {"docx", "xlsx", "xlsm", "xls", "doc"}:
            return ext_fmt
        # A real zip with no office extension is unsupported content.
        return _MIME_FORMAT.get(mime, "unknown")

    # 2) Ambiguous text: libmagic says text/plain for csv/json/txt/md/html.
    if mime in _AMBIGUOUS_TEXT_MIMES:
        # A legacy-office extension is authoritative even over a text-looking
        # body (a mislabelled/renamed .xls or .doc must still be recognised so
        # the router can reject/route it correctly).
        if ext_fmt in {"xls", "doc", "xlsx", "xlsm", "docx"}:
            return ext_fmt
        if ext_fmt in {"csv", "json", "jsonl", "txt", "md", "html"}:
            return ext_fmt
        # No helpful extension: only call it text if it both decodes as UTF-8
        # AND carries no NUL byte (a strong binary signal). Otherwise it is
        # opaque content we cannot support -> unknown (router REJECTs, rule 99).
        if b"\x00" in head_bytes:
            return "unknown"
        try:
            head_bytes.decode("utf-8")
            return "txt"
        except UnicodeDecodeError:
            return "unknown"

    # 3) Otherwise trust the MIME when we recognise it.
    by_mime = _MIME_FORMAT.get(mime)
    if by_mime:
        return by_mime

    # 4) Last resort: the extension, else unknown.
    return ext_fmt or "unknown"


def cheap_features(
    file_id: str, name: str, size_bytes: int, head_bytes: bytes
) -> DocumentFeatures:
    """Build features from CHEAP signals only (format, size, MIME) - no parse.

    This is the first thing the multi-file normaliser computes for every file so
    the size/format rules can reject or route it without ever downloading or
    parsing the whole object (B2.3). PDF geometry stays unset until
    :func:`ensure_pdf_geometry` is called for a PDF that survives the cheap gates.
    """
    return DocumentFeatures(
        file_id=file_id,
        detected_mime=detect_mime(head_bytes),
        size_bytes=size_bytes,
        format=detect_format(name, head_bytes),
    )


def ensure_pdf_geometry(features: DocumentFeatures, data: bytes) -> DocumentFeatures:
    """Populate the expensive PDF/DOCX geometry fields in place, once.

    Idempotent: a second call is a no-op (``geometry_computed`` guards it). Called
    lazily by the router/normaliser only when a geometry-dependent rule must be
    evaluated for a file that already passed the cheap gates.
    """
    if features.geometry_computed:
        return features
    if features.format == "pdf":
        _extract_pdf_features(data, features)
    elif features.format == "docx":
        _extract_docx_features(data, features)
    features.geometry_computed = True
    return features


def extract_features(file_id: str, data: bytes, name: str | None = None) -> DocumentFeatures:
    """Extract ALL router-relevant features from a document's bytes (eager).

    Convenience wrapper that does the cheap pass then forces geometry. Kept for
    tests and any caller that already holds the full bytes. Never raises on a bad
    document: a PDF that cannot be opened is reported via
    ``pdf_encrypted_or_unreadable`` so the router can REJECT it.
    """
    features = cheap_features(file_id, name or file_id, len(data), data)
    return ensure_pdf_geometry(features, data)


def _extract_pdf_features(data: bytes, features: DocumentFeatures) -> None:
    """Measure PDF geometry: per-page char counts, image area, tables, columns.

    One pdfplumber pass covers every PDF signal. pdfplumber is built on
    pdfminer.six (MIT), which raises on an encrypted file we cannot open, so an
    unreadable/encrypted PDF is reported via ``pdf_encrypted_or_unreadable`` and
    the router rejects it - one bad file must never kill the run (spec 7.4).
    """
    try:
        import pdfplumber
    except Exception:  # pragma: no cover
        features.pdf_encrypted_or_unreadable = True
        features.extra["pdf_error"] = "pdfplumber_unavailable"
        return

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        # pdfminer raises PDFPasswordIncorrect for encrypted files and various
        # PDFSyntaxError subclasses for corrupt ones; both mean "cannot read".
        features.pdf_encrypted_or_unreadable = True
        features.extra["pdf_error"] = f"open_failed: {type(exc).__name__}"
        return

    min_chars: int | None = None
    max_image_ratio = 0.0
    multi_column = False
    detected_tables = 0
    try:
        features.page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            char_count = len(text.strip())
            min_chars = char_count if min_chars is None else min(min_chars, char_count)

            page_area = float(page.width * page.height) or 1.0
            image_area = 0.0
            for img in page.images:
                try:
                    width = float(img.get("width") or (img["x1"] - img["x0"]))
                    height = float(img.get("height") or (img["y1"] - img["y0"]))
                    image_area += abs(width * height)
                except Exception:
                    continue
            max_image_ratio = max(max_image_ratio, min(1.0, image_area / page_area))

            detected_tables += len(page.find_tables())

            if _looks_multi_column(page):
                multi_column = True
    finally:
        pdf.close()

    features.min_page_chars = min_chars if min_chars is not None else 0
    features.max_page_image_area_ratio = round(max_image_ratio, 4)
    features.multi_column = multi_column
    features.detected_tables = detected_tables


def _looks_multi_column(page: Any) -> bool:
    """Heuristic: a clean vertical gutter down the page centre.

    pdfplumber words carry ``x0``/``x1``/``top`` in PDF points. We group words
    into text lines (by their ``top`` coordinate) and look for a *central
    gutter*: a line counts only if it has a word ending left of centre AND a word
    starting right of centre AND no word straddling the midline. Requiring this
    on several lines distinguishes a true two-column layout from a single wide
    paragraph (whose words span the centre) - a word-count-per-half check alone
    gives false positives on ordinary prose.
    """
    try:
        words = page.extract_words()
    except Exception:  # pragma: no cover
        return False
    if len(words) < 6:
        return False
    mid = float(page.width) / 2.0

    lines: dict[int, list[Any]] = {}
    for word in words:
        key = round(float(word["top"]))
        lines.setdefault(key, []).append(word)

    gutter_lines = 0
    for line_words in lines.values():
        ends_left = any(float(w["x1"]) < mid for w in line_words)
        starts_right = any(float(w["x0"]) > mid for w in line_words)
        straddles = any(float(w["x0"]) < mid < float(w["x1"]) for w in line_words)
        if ends_left and starts_right and not straddles:
            gutter_lines += 1
    return gutter_lines >= 3


def _extract_docx_features(data: bytes, features: DocumentFeatures) -> None:
    """Count embedded images and complex (merged/nested) tables in a DOCX."""
    try:
        import docx  # python-docx
    except Exception:  # pragma: no cover
        features.extra["docx_error"] = "python_docx_unavailable"
        return
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        features.extra["docx_error"] = f"open_failed: {type(exc).__name__}"
        return

    # Embedded images live in the package relationships as image parts.
    images = sum(
        1 for rel in document.part.rels.values() if "image" in rel.reltype
    )
    features.embedded_images = images

    complex_tables = 0
    for table in document.tables:
        if _docx_table_is_complex(table):
            complex_tables += 1
    features.complex_tables = complex_tables


def _docx_table_is_complex(table: Any) -> bool:
    """A table is 'complex' if it has merged cells (nested/merged -> BDA; rule 3)."""
    seen: set[int] = set()
    for row in table.rows:
        for cell in row.cells:
            # Merged cells share the same underlying <w:tc> element id.
            cell_id = id(cell._tc)
            if cell_id in seen:
                return True
            seen.add(cell_id)
    return False
