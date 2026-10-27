"""Unit tests for multi-file normalisation (Part B B3): discovery, routing,
dedupe, skip-unchanged, partial-success aggregation, determinism.

All offline in fake mode.
"""

from __future__ import annotations

from types import SimpleNamespace

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.documents.bda import BdaProcessor
from kb_agent.documents.multifile import MultiFileNormalizer, discover
from kb_agent.documents.outcomes import aggregate_run_status
from tests.pdf_fixtures import make_empty_pdf, make_text_pdf

CONFIG = load_config()


def _normalizer(store, run_id="r", kb_prefix="runs/r/kb-docs", force_bda=False):
    return MultiFileNormalizer(
        store, BdaProcessor(_fake_bda(store), store), CONFIG,
        run_id=run_id, kb_prefix=kb_prefix, force_bda=force_bda,
    )


def _fake_bda(store):
    cs = build_fake_client_set(bucket="fake-bucket")
    return cs.bda


def _src(ai_kwargs):
    base = {"source_uri": None, "document_uris": []}
    base.update(ai_kwargs)
    return SimpleNamespace(**base)


# --- discovery ---------------------------------------------------------------


def test_discover_sorts_and_ignores_noise() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    for name in ["src/z.txt", "src/a.txt", "src/.DS_Store", "src/__MACOSX/x", "src/Thumbs.db"]:
        S.put_text(f"uploads/r/{name}", "body")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, ignored = discover(S, ai, max_files=100)
    names = [d.name for d in discovered]
    assert names == sorted(names)  # byte-wise sorted
    assert set(names) == {"a.txt", "z.txt"}
    ignored_names = {o.name for o in ignored}
    assert ".DS_Store" in ignored_names and "Thumbs.db" in ignored_names
    assert all(o.status == "ignored" for o in ignored)


def test_discover_caps_file_count() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    for i in range(5):
        S.put_text(f"uploads/r/src/f{i}.txt", f"body {i}")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, ignored = discover(S, ai, max_files=3)
    assert len(discovered) == 3
    rejected = [o for o in ignored if o.status == "rejected"]
    assert len(rejected) == 2
    assert all("file limit" in o.status_reason for o in rejected)


def test_discover_single_object_source_uri() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_text("uploads/r/only.txt", "body")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/only.txt"})
    discovered, _ = discover(S, ai, max_files=100)
    assert len(discovered) == 1 and discovered[0].name == "only.txt"


def test_discover_document_uris_fallback() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_text("uploads/r/a.txt", "body")
    ai = _src({"document_uris": ["s3://fake-bucket/uploads/r/a.txt"]})
    discovered, _ = discover(S, ai, max_files=100)
    assert len(discovered) == 1 and discovered[0].name == "a.txt"


# --- routing + dedupe --------------------------------------------------------


def test_routes_and_dedupe() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_bytes("uploads/r/src/doc.pdf", make_text_pdf())
    S.put_bytes("uploads/r/src/scan.pdf", make_empty_pdf(2))
    S.put_text("uploads/r/src/data.csv", "a,b\n1,2\n")
    S.put_text("uploads/r/src/cfg.json", '{"k":1}')
    S.put_text("uploads/r/src/dup1.txt", "identical")
    S.put_text("uploads/r/src/dup2.txt", "identical")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, _ = discover(S, ai, max_files=100)
    result = _normalizer(S).run(discovered)
    by = {o.name: o for o in result.outcomes}
    assert by["doc.pdf"].route == "default" and by["doc.pdf"].rule_id == 10
    assert by["scan.pdf"].route == "bda" and by["scan.pdf"].rule_id == 6
    assert by["data.csv"].route == "convert" and by["data.csv"].rule_id == 12
    assert by["cfg.json"].route == "convert" and by["cfg.json"].rule_id == 11
    # dedupe: sorted-first (dup1) wins, dup2 is skipped_duplicate
    assert by["dup1.txt"].status == "staged"
    assert by["dup2.txt"].status == "skipped_duplicate"
    assert "dup1.txt" in (by["dup2.txt"].status_reason or "")


def test_convert_writes_markdown_with_original_extension() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_text("uploads/r/src/data.csv", "a,b\n1,2\n")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, _ = discover(S, ai, max_files=100)
    _normalizer(S).run(discovered)
    assert S.exists("runs/r/kb-docs/data.csv.md")  # orig ext kept, .md appended
    assert S.exists("runs/r/extractions/data.csv.md")  # per-run copy (B4.3)


def test_reject_and_ignore_excluded_from_staged() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_text("uploads/r/src/bad.xls", "legacy")
    S.put_text("uploads/r/src/good.txt", "body")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, _ = discover(S, ai, max_files=100)
    result = _normalizer(S).run(discovered)
    by = {o.name: o for o in result.outcomes}
    assert by["bad.xls"].status == "rejected" and by["bad.xls"].rule_id == 19
    assert by["good.txt"].status == "staged"
    assert len(result.staged) == 1


# --- skip-unchanged ----------------------------------------------------------


def test_skip_unchanged_on_identical_destination() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_text("uploads/r/src/a.txt", "body")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, _ = discover(S, ai, max_files=100)
    # First run stages it.
    r1 = _normalizer(S).run(discovered)
    assert r1.outcomes[0].status == "staged"
    # Second run: destination already holds the same content hash (fake compares
    # bytes) -> unchanged.
    r2 = _normalizer(S).run(discovered)
    assert r2.outcomes[0].status == "unchanged"


# --- run-level aggregation ---------------------------------------------------


def test_partial_failure_is_completed_with_warnings() -> None:
    outcomes_ok, warnings = aggregate_run_status([
        _outcome("a.txt", "staged"),
        _outcome("bad.xls", "rejected", "legacy .xls"),
    ])
    assert outcomes_ok == "ok"
    assert any("bad.xls" in w for w in warnings)


def test_all_failed_is_no_documents_ingested() -> None:
    disposition, _ = aggregate_run_status([
        _outcome("bad.xls", "rejected", "legacy"),
        _outcome("x.bin", "rejected", "unsupported"),
        _outcome(".DS_Store", "ignored"),
    ])
    assert disposition == "no_documents_ingested"


# --- determinism regardless of completion order ------------------------------


def test_outcomes_deterministic_order() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    for n in ["m.txt", "a.txt", "z.txt", "c.csv"]:
        S.put_text(f"uploads/r/src/{n}", "x,y\n1,2\n" if n.endswith(".csv") else "body")
    ai = _src({"source_uri": "s3://fake-bucket/uploads/r/src/"})
    discovered, _ = discover(S, ai, max_files=100)
    names1 = [o.name for o in _normalizer(S).run(discovered).outcomes]
    cs2 = build_fake_client_set(bucket="fake-bucket")
    for n in ["m.txt", "a.txt", "z.txt", "c.csv"]:
        cs2.store.put_text(f"uploads/r/src/{n}", "x,y\n1,2\n" if n.endswith(".csv") else "body")
    d2, _ = discover(cs2.store, ai, max_files=100)
    names2 = [o.name for o in _normalizer(cs2.store).run(d2).outcomes]
    assert names1 == names2 == sorted(names1)


# --- helpers -----------------------------------------------------------------


def _outcome(name, status, reason=None):
    from kb_agent.documents.outcomes import FileOutcome

    return FileOutcome(name=name, source_uri=f"s3://b/{name}", status=status, status_reason=reason)
