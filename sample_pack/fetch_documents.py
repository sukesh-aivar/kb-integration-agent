"""Fetch + prepare sample-pack documents from Nextcloud's manual (CC BY 3.0).

Monday's deliverable asks for OSS product documentation (60-150 pages), a mix of
formats, and some scanned-to-PDF fixtures that exercise the BDA route (spec 1.4,
4.2). This script:

* Reads Nextcloud user-manual ``.rst`` pages (licence CC BY 3.0 - redistributable
  with attribution) from a local checkout of https://github.com/nextcloud/documentation
  and converts a selection to clean Markdown/TXT/HTML under ``documents/``.
* Renders a couple of pages to **image-only (scanned-style) PDFs** so the router
  has real files that must escalate to BDA (rule 6: low extracted chars).
* Appends provenance rows (source URL, licence, download date) to ``LICENSES.md``.

It is a one-shot preparation helper, NOT part of the agent runtime. Re-running it
is safe (it overwrites the generated files). It does NOT fabricate content - it
only converts real, licensed source documents.

.. note::

    DEV-ONLY, requires PyMuPDF (``pip install -r requirements-dev.txt``). The
    scanned/degraded-PDF fixtures are produced by rasterising text to a pixmap
    with added noise and skew - that is genuine image synthesis, which the
    runtime's read-only PDF stack (pdfplumber/pdfminer.six) cannot do. PyMuPDF
    was removed from the agent runtime in Part B (it is no longer in
    requirements.txt and is excluded from the image), so it now lives in
    requirements-dev.txt purely for regenerating these committed fixtures. The
    agent itself never imports ``fitz``.

Rocket.Chat was intentionally NOT bundled: its docs repo is archived with no
declared licence, so redistribution rights are unclear (recorded in LICENSES.md).

Usage:
    # 1. Clone the source (shallow, sparse) somewhere, e.g.:
    #    git clone --depth 1 --filter=blob:none --sparse \\
    #      https://github.com/nextcloud/documentation.git /tmp/nc-docs
    #    (cd /tmp/nc-docs && git sparse-checkout set user_manual)
    # 2. python sample_pack/fetch_documents.py --source /tmp/nc-docs/user_manual
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "documents"
LICENSES = HERE / "LICENSES.md"

NEXTCLOUD_REPO = "https://github.com/nextcloud/documentation"
NEXTCLOUD_LICENCE = "CC BY 3.0"

# A curated selection giving a mix of formats + topics that map loosely to the
# planted ticket intents (so coverage/recall have something to find). Kept small
# and within the 60-150 page guidance when rendered.
SELECTION = [
    ("files/access_webgui.rst", "md"),
    ("files/quota.rst", "md"),
    ("files/sharing.rst", "md"),
    ("session_management.rst", "md"),
    ("user_2fa.rst", "md"),
    ("userpreferences.rst", "txt"),
    ("desktop/installation.rst", "md"),
    ("desktop/usage.rst", "md"),
    ("desktop/conflicts.rst", "html"),
    ("desktop/faq.rst", "md"),
]

# These are rendered to scanned-style image-only PDFs to exercise the BDA route.
SCANNED_SELECTION = [
    ("files/sharing.rst", "sharing_scanned.pdf"),
    ("user_2fa.rst", "two_factor_scanned.pdf"),
]


def _rst_to_text(rst: str) -> str:
    """Very small RST -> plain text reduction (enough for retrieval content).

    Strips directive lines and over/underline adornments; keeps headings and body
    text. We are not building a full RST parser - the KB only needs readable prose.
    """
    out_lines: list[str] = []
    for line in rst.splitlines():
        if re.match(r"^\s*\.\.\s", line):  # directive
            continue
        if re.match(r"^[=\-~^\"'`#*+]{3,}\s*$", line):  # section adornment
            continue
        line = re.sub(r":\w+:`([^`]+)`", r"\1", line)  # roles like :ref:`x`
        line = line.replace("``", "`")
        out_lines.append(line)
    text = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _as_markdown(title: str, body: str) -> str:
    return f"# {title}\n\n{body}\n"


def _as_html(title: str, body: str) -> str:
    paras = "".join(f"<p>{p.strip()}</p>\n" for p in body.split("\n\n") if p.strip())
    return (
        f"<!doctype html>\n<html><head><title>{title}</title></head>\n"
        f"<body>\n<h1>{title}</h1>\n{paras}</body></html>\n"
    )


def _title_from(rel_path: str) -> str:
    stem = Path(rel_path).stem.replace("_", " ").title()
    return f"Nextcloud - {stem}"


def _render_scanned_pdf(text: str, out_path: Path, *, degraded: bool = False) -> None:
    """Render text to an IMAGE-ONLY PDF (no text layer) to force the BDA route.

    We draw the text onto a pixmap and insert it as a full-page image, so PyMuPDF
    extracts ~0 characters from the page -> router rule 6 (low chars) -> BDA.

    When ``degraded`` is True, produce a genuinely poor scan (approval condition
    6): low resolution, added noise, and a slight skew/rotation - the kind of
    real-world scan where local extraction is hopeless and BDA/OCR is required.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    tmp = fitz.open()
    tpage = tmp.new_page(width=612, height=792)
    tpage.insert_textbox(fitz.Rect(40, 40, 572, 752), text[:3000], fontsize=10)

    if not degraded:
        pix = tpage.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        page.insert_image(fitz.Rect(0, 0, 612, 792), stream=img_bytes)
    else:
        # Low DPI + a slight skew applied via the render matrix (insert_image's
        # rotate only accepts multiples of 90, so the skew is baked into the
        # rasterised image here), then salt-and-pepper noise.
        skew = fitz.Matrix(70 / 72.0, 70 / 72.0).prerotate(3)
        img_bytes = _degrade_pixmap(tpage.get_pixmap(matrix=skew))
        page.insert_image(fitz.Rect(0, 0, 612, 792), stream=img_bytes)

    doc.save(out_path)
    doc.close()
    tmp.close()


def _degrade_pixmap(pix: object) -> bytes:
    """Add salt-and-pepper noise to a rendered page to simulate a poor scan."""
    import random

    import fitz  # noqa: F401  (pix is a fitz.Pixmap)

    rng = random.Random(42)
    samples = bytearray(pix.samples)  # type: ignore[attr-defined]
    n = pix.width * pix.height * pix.n  # type: ignore[attr-defined]
    # Flip ~6% of bytes to a random value -> speckle noise.
    for _ in range(int(n * 0.06)):
        samples[rng.randrange(n)] = rng.randrange(256)
    noisy = fitz.Pixmap(pix.colorspace, pix.irect, pix.alpha)  # type: ignore[attr-defined]
    noisy.set_rect(noisy.irect, bytes(samples[: noisy.width * noisy.height * noisy.n]))
    return noisy.tobytes("png")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare sample-pack documents.")
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to a local checkout of nextcloud/documentation/user_manual",
    )
    args = parser.parse_args()
    source: Path = args.source
    if not source.exists():
        print(f"source not found: {source}. Clone the repo first (see module docstring).")
        return 1

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    provenance: list[tuple[str, str]] = []
    written = 0

    for rel_path, fmt in SELECTION:
        src = source / rel_path
        if not src.exists():
            print(f"skip (missing): {rel_path}")
            continue
        title = _title_from(rel_path)
        body = _rst_to_text(src.read_text(encoding="utf-8"))
        name = Path(rel_path).stem
        if fmt == "md":
            (DOCS_DIR / f"{name}.md").write_text(_as_markdown(title, body), encoding="utf-8")
            out = f"{name}.md"
        elif fmt == "txt":
            (DOCS_DIR / f"{name}.txt").write_text(f"{title}\n\n{body}\n", encoding="utf-8")
            out = f"{name}.txt"
        else:
            (DOCS_DIR / f"{name}.html").write_text(_as_html(title, body), encoding="utf-8")
            out = f"{name}.html"
        provenance.append((out, f"{NEXTCLOUD_REPO}/blob/master/user_manual/{rel_path}"))
        written += 1

    for rel_path, out_name in SCANNED_SELECTION:
        src = source / rel_path
        if not src.exists():
            continue
        body = _rst_to_text(src.read_text(encoding="utf-8"))
        _render_scanned_pdf(body, DOCS_DIR / out_name)
        provenance.append(
            (
                out_name,
                f"{NEXTCLOUD_REPO}/blob/master/user_manual/{rel_path} "
                "(rendered image-only PDF)",
            )
        )
        written += 1

    # One genuinely DEGRADED scan (low-res + noise + skew) - approval condition 6.
    degraded_src = source / "files" / "sharing.rst"
    if degraded_src.exists():
        body = _rst_to_text(degraded_src.read_text(encoding="utf-8"))
        _render_scanned_pdf(body, DOCS_DIR / "sharing_degraded_scan.pdf", degraded=True)
        provenance.append(
            (
                "sharing_degraded_scan.pdf",
                f"{NEXTCLOUD_REPO}/blob/master/user_manual/files/sharing.rst "
                "(rendered DEGRADED image-only PDF: low-res, noise, skew)",
            )
        )
        written += 1

    _append_licenses(provenance, today)
    print(f"Wrote {written} document(s) to {DOCS_DIR} and updated {LICENSES.name}.")
    return 0


def _append_licenses(provenance: list[tuple[str, str]], date: str) -> None:
    """Write the provenance block to LICENSES.md, REPLACING any existing one.

    Idempotent: re-running the fetcher replaces the "Fetched documents" block
    rather than appending a duplicate.
    """
    existing = LICENSES.read_text(encoding="utf-8") if LICENSES.exists() else ""
    marker = "## Fetched documents"
    if marker in existing:
        existing = existing[: existing.index(marker)].rstrip() + "\n"
        LICENSES.write_text(existing, encoding="utf-8")

    lines = ["", f"## Fetched documents (added {date})", ""]
    lines.append("| File | Source | Licence | Download date |")
    lines.append("|------|--------|---------|---------------|")
    for name, url in provenance:
        lines.append(f"| `{name}` | {url} | {NEXTCLOUD_LICENCE} | {date} |")
    lines.append("")
    lines.append(
        "Rocket.Chat documentation was NOT bundled: its `docs` repo is archived "
        "with no declared licence, so redistribution rights are unclear. If a "
        "licensed Rocket.Chat source is confirmed, add it the same way."
    )
    lines.append("")
    with LICENSES.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
