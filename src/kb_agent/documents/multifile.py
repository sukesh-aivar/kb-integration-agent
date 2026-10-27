"""Multi-file normalisation engine (Part B, B3).

Turns one source (a prefix of many objects, or a single object) into a set of
KB-ready files plus a per-file outcome for each. The design goals and why they
matter (B3.1):

* Route everything (cheap, deterministic) before any slow work.
* One failed file never fails the run - the normal case is messy uploads.
* Sorted + content-addressed + idempotent: the same upload twice gives the same
  keys and results; a returning customer adding one file re-processes only it.
* Overlap latency: BDA jobs are minutes, so files are processed in a bounded
  pool; a ``max_workers``/``bda_concurrency`` of 1 degrades to sequential safely.

This module is pure orchestration over the injected ``ObjectStore`` + ``BdaClient``
(via :class:`~kb_agent.documents.bda.BdaProcessor`); it does no AWS wiring itself,
so it runs identically under the fakes. It writes nothing that contains file
content into logs, events or outcomes - only names, sizes, hashes and reasons.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kb_agent.aws.interfaces import BdaResult, ObjectStore
from kb_agent.config import AppConfig
from kb_agent.documents import convert as convert_mod
from kb_agent.documents.features import cheap_features, ensure_pdf_geometry
from kb_agent.documents.outcomes import FileOutcome, NormalizationResult
from kb_agent.documents.router import route_document
from kb_agent.documents.staging import is_ignored_name
from kb_agent.logging_setup import get_logger
from kb_agent.models import DocumentMetadata, Route, SourceType

logger = get_logger(__name__)

# How many leading bytes are enough to detect format cheaply (header + a little).
_HEAD_BYTES = 8192


@dataclass
class _Discovered:
    """One source object found under the input URI."""

    name: str  # relative path (for a prefix) or base name (for a single object)
    key: str  # the object key within its bucket
    src_uri: str  # full s3://bucket/key the caller's data lives at (for BDA)


class MultiFileNormalizer:
    """Discovers, routes and processes many source objects into KB-ready files."""

    def __init__(
        self,
        store: ObjectStore,
        bda_processor: Any,
        config: AppConfig,
        *,
        run_id: str,
        kb_prefix: str,
        owner_id: str | None = None,
        force_bda: bool = False,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._bda = bda_processor
        self._config = config
        self._run_id = run_id
        self._kb_prefix = kb_prefix  # destination prefix the KB data source reads
        self._owner_id = owner_id
        self._force_bda = force_bda
        self._emit = emit or (lambda _msg: None)

    # --- public entry --------------------------------------------------------

    def run(self, discovered: list[_Discovered]) -> NormalizationResult:
        """Route then process every discovered object; return per-file outcomes.

        ``discovered`` is already sorted by key (caller's responsibility) so
        results are deterministic regardless of completion order.
        """
        # Pass 1: cheap features + routing + dedupe. No slow work yet.
        plans: list[_FilePlan] = []
        seen_hashes: dict[str, str] = {}  # content_hash -> winning file name
        outcomes: list[FileOutcome] = []

        for d in discovered:
            try:
                data = self._store.get_bytes(d.key)
            except KeyError:
                outcomes.append(FileOutcome(
                    name=d.name, source_uri=d.src_uri, status="failed",
                    status_reason="source object not found",
                ))
                continue

            content_hash = hashlib.sha256(data).hexdigest()
            features = cheap_features(d.name, d.name, len(data), data[:_HEAD_BYTES])

            # Dedupe within the run: first (sorted) file wins.
            if content_hash in seen_hashes:
                outcomes.append(FileOutcome(
                    name=d.name, source_uri=d.src_uri, size=len(data),
                    format=features.format, content_hash=f"sha256:{content_hash}",
                    status="skipped_duplicate",
                    status_reason=f"identical content to {seen_hashes[content_hash]}",
                ))
                continue
            seen_hashes[content_hash] = d.name

            # Geometry is needed only for PDF/DOCX; compute lazily, once, and only
            # for a file that is not already over the hard ceiling.
            if features.format in ("pdf", "docx") and not self._over_hard_ceiling(features):
                ensure_pdf_geometry(features, data)

            decision = route_document(features, self._config)
            # force_bda (CLI/test) forces BDA, but never for structured formats
            # BDA cannot accept (those still CONVERT).
            structured = {"csv", "json", "jsonl", "xls", "xlsx", "xlsm"}
            route = (
                Route.BDA
                if self._force_bda and features.format not in structured
                else decision.route
            )

            outcome = FileOutcome(
                name=d.name, source_uri=d.src_uri, size=len(data),
                format=features.format, route=route.value.lower(),
                rule_id=decision.rule_id, reason=decision.reason,
                pages=(features.page_count or None),
                content_hash=f"sha256:{content_hash}",
            )
            if route is Route.REJECT:
                # Set the terminal status now; REJECT files skip pass-2 entirely.
                outcome.status = "rejected"
                outcome.status_reason = decision.reason
            self._emit_routing(outcome, route)
            plans.append(_FilePlan(discovered=d, data=data, route=route, outcome=outcome))

        # Pass 2: do the slow work in a bounded pool (BDA + local). Each plan
        # mutates its own outcome; results are collected in the sorted plan order.
        self._process(plans)

        for plan in plans:
            outcomes.append(plan.outcome)

        # Re-sort outcomes by name so the record order is deterministic.
        outcomes.sort(key=lambda o: o.name)
        return NormalizationResult(outcomes=outcomes)

    # --- pass 2 processing ---------------------------------------------------

    def _process(self, plans: list[_FilePlan]) -> None:
        workers = max(1, int(self._config.processing.max_workers))
        # Submit BDA-bound work first so it overlaps with local work. A pool size
        # of 1 (or a single file) degrades to sequential safely.
        to_run = [p for p in plans if p.route is not Route.REJECT]
        if not to_run:
            return
        if workers == 1 or len(to_run) == 1:
            for plan in to_run:
                self._process_one(plan)
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # BDA first, then convert/default.
            ordered = sorted(to_run, key=lambda p: 0 if p.route is Route.BDA else 1)
            list(pool.map(self._process_one, ordered))

    def _process_one(self, plan: _FilePlan) -> None:
        o = plan.outcome
        try:
            if plan.route is Route.REJECT:
                o.status = "rejected"
                o.status_reason = plan.outcome.reason
                return
            if plan.route is Route.BDA:
                self._do_bda(plan)
            elif plan.route is Route.CONVERT:
                self._do_convert(plan)
            else:  # DEFAULT
                self._do_default(plan)
        except convert_mod.UnsupportedFormatError as exc:
            o.status = "rejected"
            o.status_reason = str(exc)
        except convert_mod.ConversionError as exc:
            o.status = "rejected"
            o.status_reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad file never fails the run
            logger.error("normalize_file_failed", file=o.name, error=type(exc).__name__)
            o.status = "failed"
            o.status_reason = f"processing error: {type(exc).__name__}"

    def _do_bda(self, plan: _FilePlan) -> None:
        o = plan.outcome
        result: BdaResult = self._bda.process(
            plan.discovered.name, plan.data, plan.discovered.src_uri
        )
        o.bda_cached = result.cached
        o.pages = result.pages_processed or o.pages
        if result.cached:
            self._emit(f"{o.name} was parsed before, so the cached result was reused")
        else:
            self._emit(f"Parsed {result.pages_processed} pages from {o.name}")
        kb_key, extraction_uri = self._write_text_extraction(
            plan, text=result.markdown, stem=f"{o.name}.md"
        )
        o.kb_key = kb_key
        o.extraction_uri = extraction_uri
        o.status = "staged"

    def _do_convert(self, plan: _FilePlan) -> None:
        o = plan.outcome
        max_part = int(self._config.source.kb_path_max_mb) * 1024 * 1024
        result = convert_mod.convert(
            plan.discovered.name, o.format, plan.data,
            max_part_bytes=max_part, max_depth=int(self._config.json_convert.max_depth),
        )
        o.part_count = result.part_count
        # Each part becomes its own KB doc named "<original-name>.md" (or
        # "<original-name>.part-001.md" when split). The original extension stays
        # in the name (B2.6) so a converted "data.csv" -> "data.csv.md" never
        # collides with a real "data.md".
        first_kb_key: str | None = None
        first_extraction: str | None = None
        for part in result.parts:
            stem = o.name + (f".{part.suffix}" if part.suffix else "") + ".md"
            kb_key, extraction_uri = self._write_text_extraction(
                plan, text=part.markdown, stem=stem
            )
            if first_kb_key is None:
                first_kb_key, first_extraction = kb_key, extraction_uri
        o.kb_key = first_kb_key
        o.extraction_uri = first_extraction
        o.status = "staged"
        self._emit(
            f"Converted {o.name} to {result.part_count} Markdown "
            f"part{'s' if result.part_count != 1 else ''}"
        )

    def _do_default(self, plan: _FilePlan) -> None:
        o = plan.outcome
        # DEFAULT: write the RAW bytes under the KB prefix (Bedrock's parser reads
        # it). Skip-unchanged: if the destination already holds this content hash,
        # record "unchanged" and skip the write.
        kb_key = self._dest_key(o.name)
        if self._unchanged(kb_key, o.content_hash):
            o.kb_key = kb_key
            o.status = "unchanged"
            return
        self._store.put_bytes(kb_key, plan.data)
        self._write_sidecar(kb_key, plan)
        o.kb_key = kb_key
        o.status = "staged"

    # --- writers -------------------------------------------------------------

    def _write_text_extraction(self, plan: _FilePlan, *, text: str, stem: str) -> tuple[str, str]:
        """Write an extracted-text doc to the KB prefix + a per-run copy (B4.3).

        ``stem`` is the full destination file name (e.g. ``report.pdf.md`` for a
        BDA extraction, ``data.csv.md`` for a conversion).
        """
        name = stem
        kb_key = self._dest_key(name)
        self._store.put_text(kb_key, text)
        self._write_sidecar(kb_key, plan)
        # Per-run extraction copy for review (B4.3).
        extraction_key = f"runs/{self._run_id}/extractions/{_safe(name)}"
        extraction_uri = self._store.put_text(extraction_key, text)
        return kb_key, extraction_uri

    def _write_sidecar(self, doc_key: str, plan: _FilePlan) -> None:
        """Write the frozen ``<key>.metadata.json`` sidecar (B5.4: no new keys)."""
        chash = (plan.outcome.content_hash or "").removeprefix("sha256:")
        metadata = DocumentMetadata(
            source=plan.outcome.name,
            source_type=SourceType.UPLOADED_DOC,
            route=plan.route,
            content_hash=chash,
            run_id=self._run_id,
            ingested_at=datetime.now(UTC),
            language=None,
        )
        self._store.put_text(
            f"{doc_key}.metadata.json",
            json.dumps(_sidecar_payload(metadata, self._owner_id), indent=2),
        )

    # --- helpers -------------------------------------------------------------

    def _dest_key(self, name: str) -> str:
        return f"{self._kb_prefix}/{_safe(name)}"

    def _unchanged(self, kb_key: str, content_hash: str | None) -> bool:
        """True if an object already at ``kb_key`` carries the same content hash.

        Uses the store's ``head`` metadata when available; falls back to comparing
        bytes. In fake mode (no head) a differing object simply overwrites.
        """
        if not self._store.exists(kb_key) or not content_hash:
            return False
        want = content_hash.removeprefix("sha256:")
        head = getattr(self._store, "head", None)
        if callable(head):
            meta = head(kb_key) or {}
            return meta.get("content_hash") == want
        try:
            existing = hashlib.sha256(self._store.get_bytes(kb_key)).hexdigest()
            return existing == want
        except Exception:  # pragma: no cover
            return False

    def _over_hard_ceiling(self, features: Any) -> bool:
        return features.size_bytes > int(self._config.source.max_file_mb) * 1024 * 1024

    def _emit_routing(self, outcome: FileOutcome, route: Route) -> None:
        if route is Route.REJECT:
            return
        dest = {
            Route.BDA: "Bedrock Data Automation",
            Route.CONVERT: "conversion",
            Route.DEFAULT: "the default parser",
        }[route]
        self._emit(
            f"Routing {outcome.name} to {dest} "
            f"(rule {outcome.rule_id}: {outcome.reason})"
        )


@dataclass
class _FilePlan:
    """A routed file awaiting pass-2 processing."""

    discovered: _Discovered
    data: bytes
    route: Route
    outcome: FileOutcome


# --- discovery ---------------------------------------------------------------


def discover(
    store: ObjectStore, ai: Any, *, max_files: int
) -> tuple[list[_Discovered], list[FileOutcome]]:
    """Enumerate source objects from ``source_uri`` (prefix or object) or the
    deprecated ``document_uris`` fallback (B3.2 step 1-2).

    Returns ``(discovered, ignored_outcomes)``. Bookkeeping noise is dropped and
    recorded as ``ignored`` outcomes; the list is sorted by key byte-wise;
    excess files beyond ``max_files`` are rejected (recorded, not silently
    dropped).
    """
    source_uri = getattr(ai, "source_uri", None)
    pairs: list[tuple[str, str, str]] = []  # (name, key, src_uri)

    if source_uri:
        bucket, prefix = _split_s3(source_uri)
        keys = sorted(store.list_keys(prefix))
        # A single-object source_uri: list_keys returns exactly it (or nothing).
        base_prefix = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
        for key in keys:
            if base_prefix and key.startswith(base_prefix):
                rel = key[len(base_prefix):]
            else:
                rel = key.rsplit("/", 1)[-1]
            src = f"s3://{bucket}/{key}" if bucket else key
            pairs.append((rel, key, src))
    else:
        for uri in getattr(ai, "document_uris", []) or []:
            key = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri
            name = key.rsplit("/", 1)[-1]
            src = uri if uri.startswith("s3://") else key
            pairs.append((name, key, src))

    pairs.sort(key=lambda p: p[1])  # byte-wise by key

    discovered: list[_Discovered] = []
    ignored: list[FileOutcome] = []
    for name, key, src in pairs:
        if is_ignored_name(name):
            ignored.append(FileOutcome(
                name=name, source_uri=src, status="ignored",
                status_reason="bookkeeping/OS file, not customer content",
            ))
            continue
        discovered.append(_Discovered(name=name, key=key, src_uri=src))

    # Cap the file count (reject the excess, recorded with a reason).
    if len(discovered) > max_files:
        for extra in discovered[max_files:]:
            ignored.append(FileOutcome(
                name=extra.name, source_uri=extra.src_uri, status="rejected",
                status_reason=f"over the per-run file limit of {max_files}",
            ))
        discovered = discovered[:max_files]

    return discovered, ignored


def _split_s3(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key-or-prefix`` into ``(bucket, key)``; ``("", uri)`` if bare."""
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        return bucket, key
    return "", uri


def _safe(name: str) -> str:
    """Sanitise a relative path into a flat, safe destination key segment."""
    out = []
    for ch in name:
        if ch.isalnum() or ch in "._- ":
            out.append(ch)
        elif ch == "/":
            out.append("_")
        else:
            out.append("_")
    return "".join(out) or "file"


def _sidecar_payload(metadata: DocumentMetadata, owner_id: str | None) -> dict:
    """The frozen sidecar payload (same keys as Part A; B5.4 - add no new keys)."""
    attrs = {
        "source": metadata.source,
        "source_type": metadata.source_type.value,
        "route": metadata.route.value,
        "content_hash": metadata.content_hash,
        "run_id": metadata.run_id,
        "ingested_at": metadata.ingested_at.isoformat(),
        "language": metadata.language or "unknown",
    }
    if owner_id:
        attrs["owner_id"] = owner_id
    return {"metadataAttributes": attrs}
