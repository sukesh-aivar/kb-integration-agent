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
from kb_agent.obs.runstate import (
    SKIP_NOT_IN_WORKFLOW,
    compute_skip_set,
    legacy_placeholder_skip_set,
)
from kb_agent.pipeline import Pipeline
from tests.pdf_fixtures import make_empty_pdf

CONFIG = load_config()


# --- helpers -----------------------------------------------------------------


def _make_scanned_pdf(pages: int) -> bytes:
    """A text-free PDF of N pages: ~0 chars/page -> routes to BDA (rule 6)."""
    return make_empty_pdf(pages)


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


# --- 1. no page cap: max_pages is accepted and ignored (Part B decision 4) ---


def test_max_pages_is_accepted_and_ignored_no_cap() -> None:
    # Part B removes all agent-imposed page/cost caps. A legacy max_pages is still
    # accepted on the payload (back-compat) but must NOT stop BDA from running.
    cs = build_fake_client_set()
    spy = _BdaSpy(cs.bda)
    cs.bda = spy  # type: ignore[assignment]

    # One 5-page scanned PDF with a tiny legacy max_pages -> BDA still runs.
    cs.store.put_bytes("uploads/r/doc.pdf", _make_scanned_pdf(5))
    ai = AgentInput(
        run_id="r",
        document_uris=["s3://b/uploads/r/doc.pdf"],
        max_pages=3,
    )

    out = Pipeline(cs, CONFIG, force_bda=True).run(ai, emit_runstate=True)

    assert out.status != "failed"
    assert spy.calls  # BDA WAS invoked despite the small max_pages

    fin = json.loads(cs.store.get_text("runs/r/_finished.json"))
    assert fin["status"] != "failed"


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


def test_skip_set_is_empty_in_kb_only_workflow() -> None:
    # v3: the active KB-only workflow has only the four KB stages, none skipped.
    assert compute_skip_set(has_tickets=False, has_golden=False) == {}
    assert compute_skip_set(has_tickets=True, has_golden=True) == {}


def test_legacy_placeholder_skip_set_marks_the_nine_dormant_stages() -> None:
    # A4.2: with the legacy flag on, an unchanged 13-step consumer sees the nine
    # dormant stages marked skipped with the exact reason string.
    skipped = legacy_placeholder_skip_set()
    for stage in (
        "load_tickets", "adapt_tickets", "clean_tickets", "embed_texts",
        "intent_cluster", "name_clusters", "kb_retrieve", "coverage_check",
        "draft_article",
    ):
        assert skipped[stage] == SKIP_NOT_IN_WORKFLOW
    # The four active stages are never in the skip set.
    for stage in ("normalize_docs", "kb_create", "kb_ingest", "write_report"):
        assert stage not in skipped


def test_started_file_records_empty_skip_set_in_kb_only() -> None:
    cs = build_fake_client_set()
    cs.store.put_text("uploads/s/docs/a.md", "hello world content")
    Pipeline(cs, CONFIG).run(
        AgentInput(run_id="s", document_uris=["s3://b/uploads/s/docs/a.md"]),
        emit_runstate=True,
    )
    started = json.loads(cs.store.get_text("runs/s/_started.json"))
    assert started["agent_version"] == "v3"
    assert started["skipped"] == {}
    assert started["stage_order"] == ["normalize_docs", "kb_create", "kb_ingest", "write_report"]


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


# --- 9. legacy stage placeholders (A4.2) -------------------------------------


def test_legacy_placeholders_report_thirteen_with_nine_skipped() -> None:
    """With legacy_stage_placeholders on, _started reports the original 13 stages
    (nine dormant ones skipped), while execution still runs only the four."""
    cfg = load_config().model_copy(deep=True)
    cfg.workflow.legacy_stage_placeholders = True

    cs = build_fake_client_set()
    cs.store.put_text("uploads/leg/docs/a.md", "hello world content")
    Pipeline(cs, cfg).run(
        AgentInput(run_id="leg", document_uris=["s3://b/uploads/leg/docs/a.md"]),
        emit_runstate=True,
    )

    started = json.loads(cs.store.get_text("runs/leg/_started.json"))
    assert len(started["stage_order"]) == 13
    assert len(started["skipped"]) == 9
    for stage in (
        "load_tickets", "adapt_tickets", "clean_tickets", "embed_texts",
        "intent_cluster", "name_clusters", "kb_retrieve", "coverage_check",
        "draft_article",
    ):
        assert started["skipped"][stage] == SKIP_NOT_IN_WORKFLOW

    # Manifest records the 9 skipped placeholders + the 4 executed stages = 13.
    manifest = json.loads(cs.store.get_text("runs/leg/manifest.json"))
    assert len(manifest["stages"]) == 13
