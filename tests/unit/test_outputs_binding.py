"""Tests for Part B outputs: status, metrics, the binding, and _state (B4/B5)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.contract import AgentInput
from kb_agent.pipeline import Pipeline
from tests.pdf_fixtures import make_text_pdf

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "docs" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def _run(run_id="r", project_id="acme", config=None, emit=True):
    cs = build_fake_client_set(bucket="fake-bucket")
    S = cs.store
    S.put_bytes(f"uploads/{run_id}/src/a.pdf", make_text_pdf())
    S.put_text(f"uploads/{run_id}/src/d.csv", "name,role\nAlice,eng\n")
    ai = AgentInput(
        run_id=run_id, project_id=project_id,
        source_uri=f"s3://fake-bucket/uploads/{run_id}/src/",
        chunking_strategy="hierarchical",
    )
    out = Pipeline(cs, config or load_config()).run(ai, emit_runstate=emit)
    return cs, out


# --- status ------------------------------------------------------------------


def test_status_is_completed_by_default() -> None:
    _, out = _run()
    assert out.status == "completed"
    assert out.project_id == "acme"
    assert out.knowledge_base_id
    assert out.knowledge_binding_uri and out.knowledge_binding_uri.endswith(
        "knowledge_binding.json"
    )


def test_legacy_status_flag_re_emits_needs_review() -> None:
    config = load_config()
    config.workflow.legacy_status_needs_review = True
    cs, out = _run(config=config)
    assert out.status == "needs_review"
    fin = json.loads(cs.store.get_text("runs/r/_finished.json"))
    assert fin["status"] == "needs_review"


def test_all_files_fail_is_no_documents_ingested() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    cs.store.put_text("uploads/r/src/bad.xls", "legacy")
    cs.store.put_bytes("uploads/r/src/blob.bin", b"\x00\x01\x02\x03")
    out = Pipeline(cs, load_config()).run(
        AgentInput(run_id="r", project_id="acme",
                   source_uri="s3://fake-bucket/uploads/r/src/"),
        emit_runstate=True,
    )
    assert out.status == "failed"
    assert any(e.kind == "no_documents_ingested" for e in out.errors)


def test_partial_failure_completes_with_warnings() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    cs.store.put_bytes("uploads/r/src/a.pdf", make_text_pdf())  # ok
    cs.store.put_text("uploads/r/src/bad.xls", "legacy")  # rejected
    out = Pipeline(cs, load_config()).run(
        AgentInput(run_id="r", project_id="acme",
                   source_uri="s3://fake-bucket/uploads/r/src/"),
        emit_runstate=True,
    )
    assert out.status == "completed"
    assert any("bad.xls" in w for w in out.warnings)


# --- metrics -----------------------------------------------------------------


def test_metrics_populated() -> None:
    _, out = _run()
    m = out.metrics
    assert m.files_total == 2
    assert m.files_ingested == 2
    assert m.files_rejected == 0
    assert m.pages_processed is not None
    assert m.bda_cache_hits == 0


# --- binding -----------------------------------------------------------------


def test_binding_written_to_run_and_project() -> None:
    cs, _ = _run()
    assert cs.store.exists("runs/r/knowledge_binding.json")
    assert cs.store.exists("projects/acme/knowledge_binding.json")


def test_binding_is_schema_valid() -> None:
    cs, _ = _run()
    binding = json.loads(cs.store.get_text("runs/r/knowledge_binding.json"))
    jsonschema.validate(binding, _schema("knowledge_binding.v1.schema.json"))
    assert binding["contract_version"] == "1.0"
    assert binding["vector_store"]["dimension"] == 1024
    assert {d["name"] for d in binding["documents"]} == {"a.pdf", "d.csv"}
    assert all(d["status"] == "ingested" for d in binding["documents"])


# --- run-state files ---------------------------------------------------------


def test_started_state_finished_are_schema_valid() -> None:
    cs, _ = _run()
    started = json.loads(cs.store.get_text("runs/r/_started.json"))
    state = json.loads(cs.store.get_text("runs/r/_state.json"))
    finished = json.loads(cs.store.get_text("runs/r/_finished.json"))

    jsonschema.validate(started, _schema("started.v1.schema.json"))
    jsonschema.validate(state, _schema("state.v1.schema.json"))
    jsonschema.validate(finished, _schema("finished.v1.schema.json"))
    # Each per-file outcome validates against the dedicated outcome schema.
    for f in state["files"]:
        jsonschema.validate(f, _schema("file_outcome.v1.schema.json"))

    assert started["project_id"] == "acme"
    assert started["vector_store"] == "s3_vectors"
    assert state["vector_store"]["index_name"] == "kb-acme"
    assert len(state["files"]) == 2
    assert finished["status"] == "completed"
