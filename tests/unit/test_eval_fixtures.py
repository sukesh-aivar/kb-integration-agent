"""Unit tests for the eval fixture files (spec 7.5 golden tests).

Offline. These guard the structure of golden_set.jsonl and routing_fixtures.yaml
so a malformed fixture is caught before an eval run, and so every routing rule
keeps a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS = REPO_ROOT / "evals"


def _load_golden_rows() -> list[dict]:
    rows = []
    with (EVALS / "golden_set.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "_comment" in obj:  # schema/doc line, not a data row
                continue
            rows.append(obj)
    return rows


def test_golden_set_rows_have_required_fields() -> None:
    rows = _load_golden_rows()
    assert len(rows) >= 5
    for row in rows:
        assert set(row).issuperset({"id", "question", "expected_source_ids", "intent"})
        assert isinstance(row["expected_source_ids"], list)
        assert row["expected_source_ids"], "expected_source_ids must be non-empty"


def test_golden_set_rows_are_marked_placeholders() -> None:
    # Until real documents exist, every expected source id is a flagged placeholder.
    rows = _load_golden_rows()
    for row in rows:
        assert all(sid.startswith("PLACEHOLDER_") for sid in row["expected_source_ids"])


def test_golden_set_ids_unique() -> None:
    rows = _load_golden_rows()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_routing_fixtures_cover_every_rule() -> None:
    fixtures = yaml.safe_load((EVALS / "routing_fixtures.yaml").read_text())["fixtures"]
    rules = yaml.safe_load((REPO_ROOT / "config" / "routing_rules.yaml").read_text())["rules"]
    rule_ids = {r["id"] for r in rules}
    covered = {f["expected_rule_id"] for f in fixtures}
    missing = rule_ids - covered
    assert not missing, f"routing rules without a fixture: {sorted(missing)}"


def test_routing_fixtures_have_valid_routes() -> None:
    fixtures = yaml.safe_load((EVALS / "routing_fixtures.yaml").read_text())["fixtures"]
    for f in fixtures:
        assert f["expected_route"] in {"DEFAULT", "BDA", "CONVERT", "REJECT"}
        assert "features" in f and isinstance(f["features"], dict)
