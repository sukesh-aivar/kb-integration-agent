"""Project identity + deterministic resource naming (Part B B1.1, B4.1).

The ``project_id`` is the stable key everything hangs off: the S3 source prefix,
the vector index, the knowledge base, the data source, the lock, and the project
record. Because downstream (vector bucket / index / KB) names have strict
character and length rules that are IMMUTABLE after creation, naming is a pure,
property-tested function - never an ad-hoc f-string at the call site.

All helpers here are pure (no I/O), so they are cheap to test exhaustively.
"""

from __future__ import annotations

import hashlib
import re

# project_id contract (B1.1): 3-40 chars, lowercase alphanumerics and hyphens,
# must start and end with an alphanumeric (no leading/trailing hyphen).
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
PROJECT_ID_MIN = 3
PROJECT_ID_MAX = 40

# Only s3_vectors is supported at runtime (B1.1). Anything else is rejected with
# unsupported_vector_store BEFORE anything is provisioned.
SUPPORTED_VECTOR_STORES = frozenset({"s3_vectors"})


class InvalidProjectIdError(ValueError):
    """A supplied project_id does not satisfy the contract regex (HTTP 400)."""


def validate_project_id(project_id: str) -> str:
    """Return ``project_id`` if valid, else raise :class:`InvalidProjectIdError`.

    The caller maps this to HTTP 400 ``invalid_input``. We never echo anything
    but the offending value's shape in the message (the id itself is not secret).
    """
    if not PROJECT_ID_RE.match(project_id):
        raise InvalidProjectIdError(
            f"project_id must be {PROJECT_ID_MIN}-{PROJECT_ID_MAX} lowercase letters, "
            f"digits or hyphens, starting and ending with a letter or digit"
        )
    return project_id


def derive_project_id(run_id: str) -> str:
    """Derive a VALID project_id from a run_id when none was supplied (B1.1).

    Sanitising: lowercase, every invalid character to a hyphen, collapse repeats,
    trim leading/trailing hyphens, then fit the 3-40 length window. A too-long
    result is truncated and given an 8-char content hash suffix so distinct
    run_ids keep distinct project_ids; a too-short result is padded.

    The result is guaranteed to satisfy :data:`PROJECT_ID_RE` (property-tested).
    """
    lowered = run_id.lower()
    # invalid -> '-'
    cleaned = re.sub(r"[^a-z0-9-]", "-", lowered)
    # collapse runs of hyphens, trim ends
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")

    if len(cleaned) > PROJECT_ID_MAX:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
        # keep room for "-" + 8-char hash
        head = cleaned[: PROJECT_ID_MAX - 9].rstrip("-")
        cleaned = f"{head}-{digest}"

    if len(cleaned) < PROJECT_ID_MIN:
        # Pad deterministically; a short/empty run_id still yields a valid id.
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
        cleaned = (cleaned + "-" + digest).strip("-")[:PROJECT_ID_MAX]

    # Final guard: ensure start/end are alphanumeric (the trims above handle most
    # cases, but a fully-hyphen input collapses to "" -> use the hash).
    if not PROJECT_ID_RE.match(cleaned):
        cleaned = ("p" + hashlib.sha256(run_id.encode("utf-8")).hexdigest())[:PROJECT_ID_MAX]
    return cleaned


class UnsupportedVectorStoreError(ValueError):
    """The requested vector store is not supported (HTTP 400 unsupported_vector_store)."""


def normalise_vector_store(value: str | None) -> str:
    """Normalise a vector-store dropdown string and reject anything unsupported.

    Normalisation (B1.1): lowercase, non-alphanumerics to ``_``. ``None``/empty
    defaults to ``s3_vectors``. Raises :class:`UnsupportedVectorStoreError` for
    anything other than ``s3_vectors`` so the caller can return
    ``unsupported_vector_store`` BEFORE provisioning anything.
    """
    if not value:
        return "s3_vectors"
    norm = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if norm not in SUPPORTED_VECTOR_STORES:
        raise UnsupportedVectorStoreError(
            f"unsupported vector store {value!r}; only 's3_vectors' is supported"
        )
    return norm


# --- deterministic resource names (B4.1) -------------------------------------
# All of these are pure functions of project_id + config prefixes. project_id is
# already within the 3-40 / [a-z0-9-] window, and every prefix here is lowercase
# ASCII, so the composed names satisfy the S3 Vectors / Bedrock rules:
#   vector bucket / index : 3-63, [a-z0-9-] (+ '.' for index), begin/end alnum
#   KB name <= 128, data source name <= 200.


def index_name(project_id: str, *, prefix: str = "kb-") -> str:
    """S3 Vectors index name ``{prefix}{project_id}`` (same project reuses it)."""
    return f"{prefix}{project_id}"


def kb_name(project_id: str, *, prefix: str = "kb-") -> str:
    """Knowledge base name ``{prefix}{project_id}``."""
    return f"{prefix}{project_id}"


def data_source_name(project_id: str, *, prefix: str = "ds-") -> str:
    """Data source name ``{prefix}{project_id}``."""
    return f"{prefix}{project_id}"


def source_prefix(project_id: str, *, root: str = "projects") -> str:
    """The KB data-source S3 prefix that accumulates across runs for a project."""
    return f"{root}/{project_id}/kb-docs/"


def project_record_key(project_id: str, *, root: str = "projects") -> str:
    """Key of the project record (ids + settings) for fast lookup."""
    return f"{root}/{project_id}/project.json"


def lock_key(project_id: str, *, root: str = "projects") -> str:
    """Key of the per-project concurrency lock."""
    return f"{root}/{project_id}/_lock.json"


# --- S3 key-segment sanitising (B3.3) ----------------------------------------

_SEGMENT_KEEP = re.compile(r"[^A-Za-z0-9._\- ]+")


def sanitise_segment(name: str, *, max_len: int = 200) -> str:
    """Sanitise one path segment for an S3 key (B3.3).

    Keeps letters, digits, dot, dash, underscore and space; collapses everything
    else to ``_``; caps the length while preserving the extension.
    """
    base = name.rsplit("/", 1)[-1]
    cleaned = _SEGMENT_KEEP.sub("_", base)
    if len(cleaned) <= max_len:
        return cleaned or "file"
    dot = cleaned.rfind(".")
    if dot > 0:
        ext = cleaned[dot:]
        return cleaned[: max_len - len(ext)] + ext
    return cleaned[:max_len]


def collision_suffixed(name: str, content_hash: str) -> str:
    """Append ``-{first 8 of content hash}`` deterministically before the extension.

    Used when two different source paths sanitise to the same destination key
    (B3.3); the original name is always kept in the per-file outcome.
    """
    short = content_hash.removeprefix("sha256:")[:8]
    dot = name.rfind(".")
    if dot > 0:
        return f"{name[:dot]}-{short}{name[dot:]}"
    return f"{name}-{short}"
