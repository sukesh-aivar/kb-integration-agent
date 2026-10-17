"""Which files may be staged as knowledge-base documents (spec 4.2).

A knowledge base must be built only from actual customer content, not from
repository bookkeeping files (READMEs, licences, dotfiles). Staging the wrong
file pollutes retrieval and coverage - e.g. a ``documents/README.md`` explaining
the directory is not a support document and must never be ingested.

This module is the single source of truth for that rule, so ``run_local.py``, the
pipeline, and tests all agree. Allow by content-type suffix, and explicitly deny a
small set of known non-content filenames.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Content types the knowledge base is built from (spec 1.4 doc mix + 4.2 router).
ALLOWED_DOC_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm", ".pdf", ".docx"})

# Filenames that are repository bookkeeping, never customer content.
DENIED_FILENAMES = frozenset(
    {"readme.md", "readme.txt", "readme", "licenses.md", "license", "license.md", ".gitkeep"}
)


def is_stageable_document(name: str) -> bool:
    """Return True if ``name`` is an allowed KB content file (spec 4.2).

    Denies known bookkeeping files by name first, then requires an allowed content
    suffix. ``name`` may be a bare filename or a path; only the final component is
    considered.
    """
    base = PurePosixPath(name).name
    if base.lower() in DENIED_FILENAMES:
        return False
    suffix = PurePosixPath(base).suffix.lower()
    return suffix in ALLOWED_DOC_SUFFIXES
