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
# Part B routes by detect_format, not by this suffix set; kept for the legacy
# is_stageable_document check and run_local.py.
ALLOWED_DOC_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm", ".pdf", ".docx"})

# Filenames that are repository bookkeeping, never customer content.
DENIED_FILENAMES = frozenset(
    {"readme.md", "readme.txt", "readme", "licenses.md", "license", "license.md", ".gitkeep"}
)

# Archive/OS cruft that must be ignored during multi-file discovery (B3.2 step 1).
# These are recorded as ``ignored`` outcomes, never routed or rejected.
IGNORED_FILENAMES = frozenset({"thumbs.db", ".ds_store", ".gitkeep"})
IGNORED_PATH_SEGMENTS = frozenset({"__macosx"})


def is_ignored_name(name: str) -> bool:
    """Return True if ``name`` is OS/archive bookkeeping noise (B3.2).

    Ignored (not rejected): dotfiles, ``__MACOSX/`` members, ``Thumbs.db``,
    ``.DS_Store``, and the bookkeeping names already denied for staging
    (README/LICENSE/.gitkeep). Unknown *formats* are NOT ignored here - they are
    rejected visibly by the router's catch-all (rule 99).
    """
    path = PurePosixPath(name)
    base = path.name
    lower = base.lower()
    if lower in IGNORED_FILENAMES or lower in DENIED_FILENAMES:
        return True
    # A dotfile (hidden) is noise: ".env", ".DS_Store", ".keep", etc.
    if base.startswith("."):
        return True
    # Any path segment that is archive cruft (e.g. "__MACOSX/....").
    if any(seg.lower() in IGNORED_PATH_SEGMENTS for seg in path.parts):
        return True
    return False


def is_stageable_document(name: str) -> bool:
    """Return True if ``name`` is an allowed KB content file (spec 4.2, legacy).

    Denies known bookkeeping files by name first, then requires an allowed content
    suffix. ``name`` may be a bare filename or a path; only the final component is
    considered. Part B's multi-file flow uses :func:`is_ignored_name` +
    ``detect_format`` instead; this is kept for run_local.py and back-compat.
    """
    base = PurePosixPath(name).name
    if base.lower() in DENIED_FILENAMES:
        return False
    suffix = PurePosixPath(base).suffix.lower()
    return suffix in ALLOWED_DOC_SUFFIXES
