"""Tests for create-or-verify provisioning (B4.2) with the fakes."""

from __future__ import annotations

import json

import pytest

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import get_settings, load_config
from kb_agent.provisioning import (
    TAG_MANAGED_BY,
    Provisioner,
    ProvisioningConflictError,
    ProvisioningError,
)


def _provisioner(cs, config=None):
    return Provisioner(
        cs.store, cs.vector_admin, cs.knowledge_base,
        config or load_config(), get_settings(),
    )


def test_nothing_exists_creates_all() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    r = _provisioner(cs).provision("acme", "run1", chunking_strategy="hierarchical")
    actions = {s["resource"]: s["action"] for s in r.steps}
    assert actions["index"] == "created"
    assert actions["knowledge_base"] == "created"
    assert actions["data_source"] == "created"
    assert r.knowledge_base_id and r.index_arn and r.data_source_id
    assert cs.store.exists("projects/acme/project.json")


def test_everything_exists_verifies_and_reuses() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    prov = _provisioner(cs)
    r1 = prov.provision("acme", "run1", chunking_strategy="hierarchical")
    r2 = prov.provision("acme", "run2", chunking_strategy="hierarchical")
    assert r2.knowledge_base_id == r1.knowledge_base_id
    assert r2.index_arn == r1.index_arn
    actions = {s["resource"]: s["action"] for s in r2.steps}
    assert actions["index"] == "verified"
    assert actions["knowledge_base"] == "verified"


@pytest.mark.parametrize(
    "field,new_value",
    [("dimension", 512), ("distance_metric", "euclidean"), ("data_type", "float16")],
)
def test_index_mismatch_is_conflict(field: str, new_value) -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    config = load_config()
    _provisioner(cs, config).provision("acme", "run1", chunking_strategy="hierarchical")
    # Change the configured immutable field; a second run must conflict.
    setattr(config.vector_store, field, new_value)
    with pytest.raises(ProvisioningConflictError) as ei:
        _provisioner(cs, config).provision("acme", "run2", chunking_strategy="hierarchical")
    assert ei.value.resource == "index"
    assert field in ei.value.mismatches


def test_non_filterable_keys_mismatch_is_conflict() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    config = load_config()
    _provisioner(cs, config).provision("acme", "run1", chunking_strategy="hierarchical")
    config.vector_store.non_filterable_keys = ["source"]  # fewer keys
    with pytest.raises(ProvisioningConflictError) as ei:
        _provisioner(cs, config).provision("acme", "run2", chunking_strategy="hierarchical")
    assert "non_filterable_keys" in ei.value.mismatches


def test_record_present_but_kb_missing_raises() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    _provisioner(cs).provision("acme", "run1", chunking_strategy="hierarchical")
    # Delete the KB behind the record's back -> stop, do not recreate.
    rec = json.loads(cs.store.get_text("projects/acme/project.json"))
    cs.knowledge_base.delete_knowledge_base(rec["knowledge_base_id"])
    with pytest.raises(ProvisioningError):
        _provisioner(cs).provision("acme", "run2", chunking_strategy="hierarchical")


def test_resources_are_tagged() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    _provisioner(cs).provision("acme", "run1", chunking_strategy="hierarchical")
    # The index arn must carry the managed-by tag (cleanup relies on it).
    tags = cs.vector_admin._tags
    assert any(TAG_MANAGED_BY in t for t in tags.values())


def test_provisioning_disabled_uses_fallback() -> None:
    cs = build_fake_client_set(bucket="fake-bucket")
    config = load_config()
    config.provisioning.enabled = False
    r = _provisioner(cs, config).provision("acme", "run1", chunking_strategy="hierarchical")
    assert r.knowledge_base_id  # created via the fallback KB client path
    assert r.steps[0]["action"] == "fallback"
    # No project record is written in the fallback path.
    assert not cs.store.exists("projects/acme/project.json")
