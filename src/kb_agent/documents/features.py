"""Pure document feature extraction (spec 4.2, 7.4 "pure functions for logic").

This module measures a document and returns facts. It makes NO routing decision
and performs NO I/O decision - that separation is deliberate so the router
(router.py) is a pure function of these features and so each feature is testable
in isolation.

PDF geometry uses PyMuPDF (AGPL - OPEN_QUESTIONS Q13) behind a small surface so it
can be swapped for pypdf+pdfplumber if AGPL is rejected. DOCX inspection uses
python-docx. True MIME comes from python-magic (bytes, never the extension).
"""

from __future__ import annotations

import io
from typing import Any

from pydantic import BaseModel, Field


class DocumentFeatures(BaseModel):
    """Locally-measured signals the router consumes. One per file."""

    file_id: str
    detected_mime: str
    size_bytes: int
    # PDF-specific (0/empty for non-PDFs):
    pdf_encrypted_or_unreadable: bool = False
    min_page_chars: int | None = None  # fewest extracted chars on any page
    max_page_image_area_ratio: float = 0.0
    detected_tables: int = 0
    multi_column: bool = False
    page_count: int = 0
    # DOCX-specific:
    embedded_images: int = 0
    complex_tables: int = 0
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


def extract_features(file_id: str, data: bytes) -> DocumentFeatures:
    """Extract all router-relevant features from a document's bytes.

    Dispatches on detected MIME. Never raises on a bad document: a PDF that
    cannot be opened is reported via ``pdf_encrypted_or_unreadable`` so the router
    can REJECT it (spec: one bad file must not kill the run).
    """
    mime = detect_mime(data)
    short = normalise_mime(mime)
    features = DocumentFeatures(
        file_id=file_id, detected_mime=mime, size_bytes=len(data)
    )

    if short == "pdf":
        _extract_pdf_features(data, features)
    elif short == "docx":
        _extract_docx_features(data, features)
    return features


def _extract_pdf_features(data: bytes, features: DocumentFeatures) -> None:
    """Measure PDF geometry: per-page char counts, image area, tables, columns."""
    try:
        import fitz  # PyMuPDF
    except Exception:  # pragma: no cover
        features.pdf_encrypted_or_unreadable = True
        features.extra["pdf_error"] = "pymupdf_unavailable"
        return

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        features.pdf_encrypted_or_unreadable = True
        features.extra["pdf_error"] = f"open_failed: {type(exc).__name__}"
        return

    if doc.needs_pass:  # encrypted / password-protected
        features.pdf_encrypted_or_unreadable = True
        doc.close()
        return

    min_chars: int | None = None
    max_image_ratio = 0.0
    multi_column = False
    try:
        features.page_count = doc.page_count
        for page in doc:
            text = page.get_text("text") or ""
            char_count = len(text.strip())
            min_chars = char_count if min_chars is None else min(min_chars, char_count)

            page_area = float(page.rect.width * page.rect.height) or 1.0
            image_area = 0.0
            for img in page.get_images(full=True):
                try:
                    for rect in page.get_image_rects(img[0]):
                        image_area += float(rect.width * rect.height)
                except Exception:
                    continue
            max_image_ratio = max(max_image_ratio, min(1.0, image_area / page_area))

            if _looks_multi_column(page):
                multi_column = True
    finally:
        doc.close()

    features.min_page_chars = min_chars if min_chars is not None else 0
    features.max_page_image_area_ratio = round(max_image_ratio, 4)
    features.multi_column = multi_column
    features.detected_tables = _count_pdf_tables(data)


def _looks_multi_column(page: Any) -> bool:
    """Heuristic: two dense bands of text blocks on left and right halves."""
    try:
        blocks = page.get_text("blocks")
    except Exception:  # pragma: no cover
        return False
    if len(blocks) < 4:
        return False
    mid = page.rect.width / 2.0
    left = sum(1 for b in blocks if b[2] < mid)  # block right edge left of centre
    right = sum(1 for b in blocks if b[0] > mid)  # block left edge right of centre
    return left >= 2 and right >= 2


def _count_pdf_tables(data: bytes) -> int:
    """Count detected tables with pdfplumber (spec 4.2 rule 8)."""
    try:
        import pdfplumber
    except Exception:  # pragma: no cover
        return 0
    total = 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                total += len(page.find_tables())
    except Exception:
        return total
    return total


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
