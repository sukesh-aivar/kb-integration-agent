"""Run-state / async self-reporting tests (spec v2 §5.10).

These lock the console-deploy v2 behaviour the backend depends on:

1. The BDA page cap fails BEFORE the first paid BDA call.
2. ``vectors_sample.json`` can never carry a chunk from another run.
3. The ``_started`` skip set is correct for docs-only vs tickets-only inputs.
4. ``_events`` keys sort so a lexical listing is chronological.
5. Events never leak document or ticket text.
6. ``_finished.json`` is written even when a stage raises a ``BaseException``.
7. ``emit_runstate=False`` (CLI/tests default) writes NO run-state files.

Everything runs in fake mode and offline.
"""

from __future__ import annotations

import json

import pytest

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.aws.interfaces import BdaResult
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.exceptions import PageCapExceededError
from kb_agent.obs.runstate import (
    SKIP_NO_QUESTION_SET,
    SKIP_NO_TICKETS,
    compute_skip_set,
)
from kb_agent.pipeline import Pipeline

CONFIG = load_config()


# --- helpers -----------------------------------------------------------------


def _make_scanned_pdf(pages: int) -> bytes:
    """A text-free PDF of N pages: ~0 chars/page -> routes to BDA (rule 6)."""
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


class _BdaSpy:
    """Wraps the fake BDA client and records every process_document call."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def process_document(self, input_s3_uri: str, output_prefix: str) -> BdaResult:
        self.calls.append(input_s3_uri)
        return self._inner.process_document(input_s3_uri, output_prefix)


def _events(store: object, run_id: str) -> list[dict]:
    keys = store.list_keys(f"runs/{run_id}/_events/")
    return [json.loads(store.get_text(k)) for k in keys]


# --- 1. page cap fails before BDA client call --------------------------------


def test_page_cap_fails_before_any_bda_call() -> None:
    cs = build_fake_client_set()
    spy = _BdaSpy(cs.bda)
    cs.bda = spy  # type: ignore[assignment]

    # One 5-page scanned PDF; cap is 3 -> must fail before BDA runs.
    cs.store.put_bytes("uploads/r/doc.pdf", _make_scanned_pdf(5))
    ai = AgentInput(
        run_id="r",
        document_uris=["s3://b/uploads/r/doc.pdf"],
        max_pages=3,
    )

    out = Pipeline(cs, CONFIG, force_bda=True).run(ai, emit_runstate=True)

    assert out.status == "failed"
    assert out.stage == "normalize_docs"
    assert spy.calls == []  # BDA was never invoked

    # _finished.json records the page_cap_exceeded kind (spec §5.3).
    fin = json.loads(cs.store.get_text("runs/r/_finished.json"))
    assert fin["status"] == "failed"
    assert fin["error"]["kind"] == "page_cap_exceeded"


def test_page_cap_is_raised_as_page_cap_error() -> None:
    # The two-pass normalize must raise PageCapExceededError (not a BDA error),
    # so the mapping to kind=page_cap_exceeded is exercised directly.
    cs = build_fake_client_set()
    spy = _BdaSpy(cs.bda)
    cs.bda = spy  # type: ignore[assignment]
    cs.store.put_bytes("uploads/r2/doc.pdf", _make_scanned_pdf(4))
    pipe = Pipeline(cs, CONFIG, force_bda=True)
    state = type("S", (), {})()
    state._max_pages = 2  # type: ignore[attr-defined]
    # Drive the stage directly to assert the exception type precisely.
    ai = AgentInput(run_id="r2", document_uris=["s3://b/uploads/r2/doc.pdf"], max_pages=2)
    from kb_agent.pipeline import PipelineState

    st = PipelineState()
    st._max_pages = 2  # type: ignore[attr-defined]
    st._owner_id = None  # type: ignore[attr-defined]
    from kb_agent.obs.audit import RunManifest

    manifest = RunManifest(run_id="r2", config_snapshot={}, library_versions={})
    with pytest.raises(PageCapExceededError):
        pipe._stage_normalize_docs(ai, st, manifest, None)
    assert spy.calls == []


# --- 2. vectors_sample never cross-run ---------------------------------------


def test_vectors_sample_is_scoped_to_its_own_run() -> None:
    cs = build_fake_client_set()

    # Run A ingests a password doc.
    cs.store.put_text("uploads/A/docs/pw.md", "reset your password in account settings")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="A", document_uris=["s3://b/uploads/A/docs/pw.md"]),
        emit_runstate=True,
    )
    # Run B ingests a totally different doc.
    cs.store.put_text("uploads/B/docs/sync.md", "relink the desktop client to fix sync")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="B", document_uris=["s3://b/uploads/B/docs/sync.md"]),
        emit_runstate=True,
    )

    sample_a = json.loads(cs.store.get_text("runs/A/vectors_sample.json"))
    sample_b = json.loads(cs.store.get_text("runs/B/vectors_sample.json"))

    a_sources = " ".join(c.get("source_id", "") for c in sample_a.get("chunks", []))
    b_sources = " ".join(c.get("source_id", "") for c in sample_b.get("chunks", []))

    # A's sample must not contain B's run id (and vice-versa).
    assert "/B/" not in a_sources and "runs/B" not in a_sources
    assert "/A/" not in b_sources and "runs/A" not in b_sources


# --- 3. _started skip set correct --------------------------------------------


def test_skip_set_docs_only_skips_ticket_and_join_stages() -> None:
    skipped = compute_skip_set(has_tickets=False, has_golden=False)
    for stage in (
        "load_tickets", "adapt_tickets", "clean_tickets",
        "embed_texts", "intent_cluster", "name_clusters",
        "coverage_check", "draft_article",
    ):
        assert skipped[stage] == SKIP_NO_TICKETS
    assert skipped["kb_retrieve"] == SKIP_NO_QUESTION_SET
    # The red-pill + KB stages are NOT skipped when only docs are supplied.
    for stage in ("normalize_docs", "kb_create", "kb_ingest", "write_report"):
        assert stage not in skipped


def test_skip_set_with_tickets_keeps_ticket_stages() -> None:
    skipped = compute_skip_set(has_tickets=True, has_golden=False)
    for stage in ("load_tickets", "adapt_tickets", "coverage_check", "draft_article"):
        assert stage not in skipped
    # Without a question set, retrieval is still skipped.
    assert skipped["kb_retrieve"] == SKIP_NO_QUESTION_SET


def test_started_file_records_computed_skip_set() -> None:
    cs = build_fake_client_set()
    cs.store.put_text("uploads/s/docs/a.md", "hello world content")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="s", document_uris=["s3://b/uploads/s/docs/a.md"]),
        emit_runstate=True,
    )
    started = json.loads(cs.store.get_text("runs/s/_started.json"))
    assert started["agent_version"] == "v3"
    assert started["skipped"]["load_tickets"] == SKIP_NO_TICKETS
    assert "normalize_docs" not in started["skipped"]


# --- 4. event keys sort chronologically --------------------------------------


def test_event_keys_sort_chronologically() -> None:
    cs = build_fake_client_set()
    cs.store.put_text("uploads/e/docs/a.md", "content here")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="e", document_uris=["s3://b/uploads/e/docs/a.md"]),
        emit_runstate=True,
    )
    keys = cs.store.list_keys("runs/e/_events/")
    assert keys == sorted(keys)  # zero-padded seq -> lexical == chronological
    seqs = [json.loads(cs.store.get_text(k))["seq"] for k in keys]
    assert seqs == sorted(seqs) == list(range(len(seqs)))


# --- 5. no document/ticket text in events ------------------------------------


def test_events_do_not_leak_document_text() -> None:
    cs = build_fake_client_set()
    secret = "SUPERSECRETDOCUMENTBODY12345"
    cs.store.put_text("uploads/leak/docs/a.md", f"intro {secret} outro")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="leak", document_uris=["s3://b/uploads/leak/docs/a.md"]),
        emit_runstate=True,
    )
    for ev in _events(cs.store, "leak"):
        blob = json.dumps(ev)
        assert secret not in blob


# --- 6. _finished written on BaseException -----------------------------------


def test_finished_written_even_on_base_exception() -> None:
    cs = build_fake_client_set()
    cs.store.put_text("uploads/boom/docs/a.md", "content")

    pipe = Pipeline(cs, CONFIG)

    # Make a mid-pipeline stage raise a BaseException (e.g. KeyboardInterrupt).
    def _boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise KeyboardInterrupt("simulated interrupt")

    pipe._stage_kb_create = _boom  # type: ignore[assignment]

    ai = AgentInput(run_id="boom", document_uris=["s3://b/uploads/boom/docs/a.md"])
    with pytest.raises(BaseException):  # noqa: B017, PT011 - we re-raise after writing
        pipe.run(ai, emit_runstate=True)

    # The run-state writer still recorded a terminal _finished.json.
    assert cs.store.exists("runs/boom/_finished.json")
    fin = json.loads(cs.store.get_text("runs/boom/_finished.json"))
    assert fin["status"] == "failed"


# --- 7. CLI/sync default writes no run-state ---------------------------------


def test_emit_runstate_false_writes_no_runstate_files() -> None:
    cs = build_fake_client_set()
    cs.store.put_text("uploads/clean/docs/a.md", "content here")
    # Default (emit_runstate omitted) must not write any _started/_events/etc.
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="clean", document_uris=["s3://b/uploads/clean/docs/a.md"])
    )
    for leaf in ("_started.json", "_heartbeat.json", "_state.json",
                 "_finished.json", "vectors_sample.json"):
        assert not cs.store.exists(f"runs/clean/{leaf}")
    assert cs.store.list_keys("runs/clean/_events/") == []
    # But the normal manifest/report are still written.
    assert cs.store.exists("runs/clean/manifest.json")


# --- 8. BDA receives a FULL s3://bucket/key uri (regression) -----------------


def test_bda_input_uri_keeps_the_bucket_name() -> None:
    """Regression: the BDA input uri must be the caller's full s3://bucket/key.

    A bug reconstructed it as f"s3://{key}" from the bare key, dropping the
    bucket so Bedrock Data Automation tried to read s3://uploads/... and failed
    with "Unable to read file from given S3 location." Earlier smoke tests missed
    it because they hit the BDA content-cache (no real process_document call).
    """
    cs = build_fake_client_set(bucket="kb-integration-agent-test")
    spy = _BdaSpy(cs.bda)
    cs.bda = spy  # type: ignore[assignment]

    # A real uploaded doc under a full s3 uri, forced down the BDA route.
    key = "uploads/r-123/docs/doc.pdf"
    cs.store.put_bytes(key, _make_scanned_pdf(2))
    uri = f"s3://kb-integration-agent-test/{key}"

    Pipeline(cs, CONFIG, force_bda=True).run(
        AgentInput(run_id="r-123", document_uris=[uri]),
        emit_runstate=False,
    )

    assert spy.calls, "BDA should have been called (no cache entry)"
    got = spy.calls[0]
    # The bucket must be present and must NOT have been replaced by the first
    # key segment ("uploads").
    assert got == uri
    assert got.startswith("s3://kb-integration-agent-test/")
    assert not got.startswith("s3://uploads/")
