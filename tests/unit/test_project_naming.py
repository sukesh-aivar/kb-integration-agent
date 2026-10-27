"""Property + unit tests for project identity and resource naming (B1.1, B4.1)."""

from __future__ import annotations

import pytest

from kb_agent.project import (
    PROJECT_ID_RE,
    InvalidProjectIdError,
    UnsupportedVectorStoreError,
    collision_suffixed,
    data_source_name,
    derive_project_id,
    index_name,
    kb_name,
    normalise_vector_store,
    sanitise_segment,
    validate_project_id,
)


@pytest.mark.parametrize("good", ["abc", "a1b", "acme-corp", "a" * 40, "x9z"])
def test_validate_accepts_valid(good: str) -> None:
    assert validate_project_id(good) == good


@pytest.mark.parametrize("bad", ["ab", "-abc", "abc-", "AbC", "a" * 41, "a_b", "a b", ""])
def test_validate_rejects_invalid(bad: str) -> None:
    with pytest.raises(InvalidProjectIdError):
        validate_project_id(bad)


@pytest.mark.parametrize(
    "run_id",
    ["ab", "A_B_C", "x", "", "-----", "A" * 100, "run--id--", "R/w?*", "::::", "1",
     "acme-corp", "café-déjà", "../../etc"],
)
def test_derive_always_valid(run_id: str) -> None:
    pid = derive_project_id(run_id)
    assert PROJECT_ID_RE.match(pid), f"{run_id!r} -> {pid!r} invalid"
    assert 3 <= len(pid) <= 40


def test_derive_is_deterministic() -> None:
    assert derive_project_id("some-run-id") == derive_project_id("some-run-id")


def test_derive_long_ids_distinct() -> None:
    a = derive_project_id("x" * 60 + "-a")
    b = derive_project_id("x" * 60 + "-b")
    assert a != b  # hash suffix keeps distinct long ids distinct


@pytest.mark.parametrize(
    "value,expected",
    [(None, "s3_vectors"), ("", "s3_vectors"), ("s3_vectors", "s3_vectors"),
     ("S3 Vectors", "s3_vectors"), ("s3-vectors", "s3_vectors")],
)
def test_normalise_vector_store(value, expected) -> None:
    assert normalise_vector_store(value) == expected


@pytest.mark.parametrize("bad", ["opensearch_serverless", "pinecone", "OpenSearch"])
def test_normalise_vector_store_rejects_unsupported(bad: str) -> None:
    with pytest.raises(UnsupportedVectorStoreError):
        normalise_vector_store(bad)


def test_resource_names_are_within_s3_vectors_rules() -> None:
    pid = "acme-corp"
    assert index_name(pid) == "kb-acme-corp"
    assert kb_name(pid) == "kb-acme-corp"
    assert data_source_name(pid) == "ds-acme-corp"
    # index/bucket: 3-63 lowercase + hyphen + begin/end alnum
    for n in (index_name(pid), kb_name(pid)):
        assert 3 <= len(n) <= 63 and n[0].isalnum() and n[-1].isalnum()


def test_sanitise_segment_keeps_safe_chars_and_extension() -> None:
    # a run of invalid characters collapses to a single underscore
    assert sanitise_segment("weird name!@#.pdf") == "weird name_.pdf"
    long = "a" * 300 + ".pdf"
    out = sanitise_segment(long, max_len=50)
    assert len(out) <= 50 and out.endswith(".pdf")


def test_collision_suffix_before_extension() -> None:
    assert collision_suffixed("report.pdf", "sha256:abcdef0123456789") == "report-abcdef01.pdf"
    assert collision_suffixed("noext", "sha256:abcdef0123456789") == "noext-abcdef01"
