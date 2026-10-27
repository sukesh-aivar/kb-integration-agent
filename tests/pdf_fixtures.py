"""Tiny pure-Python PDF writer for tests (no PyMuPDF/AGPL, no new dependency).

Part B dropped PyMuPDF (AGPL), which was the only library that could *author*
PDFs in the test fixtures. pdfplumber/pdfminer.six is read-only, so these helpers
emit minimal, valid PDF bytes by hand - just enough for the feature-extraction
and routing tests:

* ``make_text_pdf``  - a single page with a real text layer (>100 chars) so the
  router sees it as a text document (DEFAULT route).
* ``make_empty_pdf`` - N pages with no text layer (~0 chars/page) so the router
  treats it as scanned and sends it to BDA (rule 6).

The output is a hand-built PDF 1.4 file (catalog + pages tree + one content
stream per page + a single Helvetica font). pdfplumber reads it correctly; it is
intentionally not a general-purpose PDF writer.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    """Escape the characters that are special inside a PDF string literal."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf(pages_text: list[str | None]) -> bytes:
    """Build a PDF from a list of per-page strings (None = page with no text)."""
    n_pages = len(pages_text)

    # Object id layout: 1=catalog, 2=pages tree, then (page, content) per page,
    # then one shared font object last.
    page_ids: list[int] = []
    content_ids: list[int] = []
    next_id = 3
    for _ in range(n_pages):
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    font_id = next_id

    parts: dict[int, bytes] = {}
    parts[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    parts[2] = f"<< /Type /Pages /Count {n_pages} /Kids [{kids}] >>".encode()

    for i, text in enumerate(pages_text):
        pid, cid = page_ids[i], content_ids[i]
        parts[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {cid} 0 R >>"
        ).encode()
        if text:
            stream = f"BT /F1 12 Tf 72 720 Td ({_escape(text)}) Tj ET".encode()
        else:
            stream = b""
        parts[cid] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)

    parts[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    body = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(parts):
        offsets[oid] = len(body)
        body += f"{oid} 0 obj\n".encode() + parts[oid] + b"\nendobj\n"

    xref_pos = len(body)
    total = max(parts) + 1
    body += f"xref\n0 {total}\n".encode()
    body += b"0000000000 65535 f \n"
    for oid in range(1, total):
        body += f"{offsets[oid]:010d} 00000 n \n".encode()
    body += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(body)


def make_text_pdf(text: str = "Hello world. " * 50, pages: int = 1) -> bytes:
    """A PDF whose every page carries ``text`` (a real text layer)."""
    return _build_pdf([text for _ in range(pages)])


def make_empty_pdf(pages: int = 1) -> bytes:
    """A PDF of ``pages`` pages with no text layer (~0 chars/page, scanned-like)."""
    return _build_pdf([None for _ in range(pages)])
