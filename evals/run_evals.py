"""Scored evaluation runner (spec 7.5 ``make eval``; spec 1.5 validation targets).

Computes the ground-truth-scored metrics the per-run report cannot, because they
need the hidden labels / fixtures:

* **Intent purity** vs ``labels.csv`` (target >= 0.80).
* **Silhouette, cluster count vs 20, noise share** (over-split / merge diagnostic).
* **Routing accuracy** on ``evals/routing_fixtures.yaml`` (every rule has a case).
* **PII rejection** - the seeded PII file(s) must be rejected (target 1 of 1).
* **Recall@5** on the golden set (reported, with the fake-mode caveat).
* **Planted gaps flagged (x of 3)** and **coverage false-positive rate** - only
  when an approved intent/document alignment exists (see docs/intent_alignment.md,
  pending approval). Until then these are reported as "not available" with the
  reason, rather than a fake number.

Runs in fake mode by default so it produces REAL scores offline. It FAILS LOUDLY
(non-zero exit, clear message) rather than printing fake-looking numbers when a
required input is missing. Results are written to ``evals/results/{timestamp}.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from evals.metrics import (  # noqa: E402
    cluster_count,
    intent_purity,
    noise_share,
    recall_at_k,
    routing_accuracy,
    silhouette,
)
from kb_agent.aws.clientset import build_client_set  # noqa: E402
from kb_agent.config import get_settings, load_config, load_routing_rules  # noqa: E402
from kb_agent.documents.features import DocumentFeatures  # noqa: E402
from kb_agent.documents.router import route_document  # noqa: E402
from kb_agent.documents.validate import validate_text  # noqa: E402

PACK = REPO_ROOT / "sample_pack" / "tickets"
DOCS = REPO_ROOT / "sample_pack" / "documents"
EVALS = REPO_ROOT / "evals"
RESULTS = EVALS / "results"


def _labels_by_id() -> dict[str, str]:
    with (PACK / "labels.csv").open(encoding="utf-8") as handle:
        return {r["ticket_id"]: r["seeded_intent"] for r in csv.DictReader(handle)}


def _edge_cases_by_id() -> dict[str, str]:
    with (PACK / "labels.csv").open(encoding="utf-8") as handle:
        return {r["ticket_id"]: r.get("edge_case", "") for r in csv.DictReader(handle)}


def _score_clustering(config, clients) -> dict:
    """Run adapt->clean->embed->cluster and score purity/silhouette/noise."""
    from kb_agent.tickets.adapters.sample import SampleAdapter
    from kb_agent.tickets.clean import clean_tickets, embedding_texts
    from kb_agent.tickets.cluster import cluster_embeddings, should_use_density_clustering
    from kb_agent.tickets.embed import embed_texts
    from kb_agent.tickets.loaders import load_tickets_bytes

    rows = load_tickets_bytes((PACK / "tickets.csv").read_bytes(), "csv")
    tickets, _ = SampleAdapter().adapt(rows)
    tickets, _ = clean_tickets(tickets)
    if not should_use_density_clustering(len(tickets), config):
        return {"available": False, "reason": "below clustering threshold"}

    vectors = embed_texts(embedding_texts(tickets), clients.embeddings)
    result = cluster_embeddings(vectors, config)
    truth = _labels_by_id()
    true_labels = [truth[t.ticket_id] for t in tickets]
    purity = intent_purity(result.labels, true_labels)
    sil = silhouette(result.reduced, result.labels, metric="euclidean")
    return {
        "available": True,
        "intent_purity": round(purity, 4),
        "intent_purity_target": 0.80,
        "intent_purity_meets_target": purity >= 0.80,
        "silhouette": round(sil, 4),
        "cluster_count": cluster_count(result.labels),
        "expected_intent_count": 20,
        "noise_share": round(noise_share(result.labels), 4),
    }


def _score_routing(config) -> dict:
    """Route every fixture and score against expected (spec 1.5)."""
    import yaml

    rules = load_routing_rules()
    fixtures = yaml.safe_load((EVALS / "routing_fixtures.yaml").read_text())["fixtures"]
    mime_map = {
        "text/plain": "text/plain", "text/markdown": "text/markdown",
        "image/png": "image/png",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf": "application/pdf",
    }
    predicted, expected = [], []
    for fx in fixtures:
        f = fx["features"]
        features = DocumentFeatures(
            file_id=fx["name"], detected_mime=mime_map[f["mime"]], size_bytes=1000,
            pdf_encrypted_or_unreadable=f.get("pdf_encrypted_or_unreadable", False),
            min_page_chars=f.get("min_page_chars"),
            max_page_image_area_ratio=f.get("max_page_image_area_ratio", 0.0),
            detected_tables=f.get("detected_tables", 0),
            multi_column=f.get("multi_column", False),
            embedded_images=f.get("embedded_images", 0),
            complex_tables=f.get("complex_tables", 0),
        )
        predicted.append(route_document(features, config, rules).route.value)
        expected.append(fx["expected_route"])
    return {
        "available": True,
        "routing_accuracy": round(routing_accuracy(predicted, expected), 4),
        "fixtures": len(fixtures),
    }


def _score_routing_heldout(config) -> dict:
    """Route the REAL fetched documents (held-out fixtures), scored separately."""
    import yaml

    rules = load_routing_rules()
    from kb_agent.documents.features import extract_features

    path = EVALS / "routing_fixtures_heldout.yaml"
    fixtures = yaml.safe_load(path.read_text())["fixtures"]
    predicted, expected, missing = [], [], []
    for fx in fixtures:
        doc = DOCS / fx["path"]
        if not doc.exists():
            missing.append(fx["path"])
            continue
        features = extract_features(doc.name, doc.read_bytes())
        predicted.append(route_document(features, config, rules).route.value)
        expected.append(fx["expected_route"])
    if not predicted:
        return {"available": False, "reason": f"no held-out docs present: {missing}"}
    return {
        "available": True,
        "routing_accuracy_heldout": round(routing_accuracy(predicted, expected), 4),
        "fixtures": len(predicted),
        "missing": missing,
        "note": "REAL fetched documents; written after the rules, scored separately",
    }


def _score_pii_adversarial(config, clients) -> dict:
    """Regex PII recall on adversarial cases (held-out, scored separately)."""
    import yaml

    from kb_agent.documents.pii import regex_pii_findings

    cases = yaml.safe_load((EVALS / "pii_adversarial.yaml").read_text())["cases"]
    detected_when_expected = 0
    expected_total = 0
    hard_caught = 0
    hard_total = 0
    details = []
    for c in cases:
        found = bool(regex_pii_findings(c["text"]))
        if c["expect_detected"]:
            expected_total += 1
            if found:
                detected_when_expected += 1
        else:
            hard_total += 1
            if found:
                hard_caught += 1
        details.append({"name": c["name"], "expect": c["expect_detected"], "detected": found})
    return {
        "available": True,
        "regex_recall_on_expected": (
            round(detected_when_expected / expected_total, 4) if expected_total else None
        ),
        "expected_cases": expected_total,
        "hard_cases_caught_by_regex": f"{hard_caught}/{hard_total}",
        "note": "hard cases are expected to be MISSED by regex and caught by "
        "Bedrock Guardrails in --mode aws (second PII layer); honest reporting of "
        "the regex layer's limits.",
        "details": details,
    }


def _score_pii_document_rejection(config, clients) -> dict:
    """File-level: the seeded PII DOCUMENT must be rejected (target 1 of 1).

    Runs validate_text (Stage B) on the seeded PII document and on a clean control.
    Reports 'x of 1 PII files rejected' and whether the clean file was wrongly
    rejected (file-level false positive).
    """
    fixtures = REPO_ROOT / "sample_pack" / "validation_fixtures"
    pii_doc = fixtures / "pii_document.md"
    clean_doc = fixtures / "clean_document.md"
    if not pii_doc.exists():
        return {"available": False, "reason": "no seeded PII document fixture"}

    pii_res = validate_text("pii_document.md", pii_doc.read_text(), config, clients.guardrail)
    pii_rejected = (not pii_res.passed) and bool(pii_res.pii_findings)

    clean_false_positive = False
    if clean_doc.exists():
        clean_res = validate_text(
            "clean_document.md", clean_doc.read_text(), config, clients.guardrail
        )
        clean_false_positive = (not clean_res.passed) and bool(clean_res.pii_findings)

    return {
        "available": True,
        "pii_files_rejected": 1 if pii_rejected else 0,
        "pii_files_total": 1,
        "target": "1 of 1 PII files rejected",
        "meets_target": pii_rejected,
        "clean_file_false_positive": clean_false_positive,
        "pii_kinds_found": sorted({f.kind for f in pii_res.pii_findings}),
    }


def _score_pii_rejection(config, clients) -> dict:
    """Ticket-level: all seeded PII TICKETS flagged by validation."""
    from kb_agent.tickets.adapters.sample import SampleAdapter
    from kb_agent.tickets.loaders import load_tickets_bytes

    rows = load_tickets_bytes((PACK / "tickets.csv").read_bytes(), "csv")
    tickets, _ = SampleAdapter().adapt(rows)
    edge = _edge_cases_by_id()
    pii_ids = [tid for tid, e in edge.items() if e == "pii"]
    if not pii_ids:
        return {"available": False, "reason": "no seeded PII tickets found"}
    by_id = {t.ticket_id: t for t in tickets}
    rejected = 0
    checked = 0
    for tid in pii_ids:
        t = by_id.get(tid)
        if not t:
            continue
        checked += 1
        res = validate_text(tid, f"{t.subject} {t.description}", config, clients.guardrail)
        if not res.passed and res.pii_findings:
            rejected += 1
    return {
        "available": True,
        "pii_files_rejected": rejected,
        "pii_files_checked": checked,
        "target": "all seeded PII rejected",
        "meets_target": checked > 0 and rejected == checked,
    }


def _score_recall(config, clients) -> dict:
    """Recall@5 on the golden set (reported with the fake-mode caveat)."""
    golden_file = EVALS / "golden_set.jsonl"
    rows = []
    for line in golden_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "_comment" in obj:
            continue
        rows.append(obj)
    placeholder = all(
        any(sid.startswith("PLACEHOLDER_") for sid in r["expected_source_ids"]) for r in rows
    )
    if placeholder:
        return {
            "available": False,
            "reason": "golden_set.jsonl holds only placeholder rows; needs human "
            "approval of candidates before recall is meaningful",
        }
    # Build + query a KB from the staged documents.
    from kb_agent.documents.knowledge_base import KnowledgeBaseStage
    from kb_agent.documents.staging import is_stageable_document

    prefix = "eval/kb-docs"
    for p in sorted(DOCS.glob("*")):
        if p.is_file() and is_stageable_document(p.name) and p.suffix != ".pdf":
            clients.store.put_text(f"{prefix}/{p.name}", p.read_text())
    stage = KnowledgeBaseStage(clients.knowledge_base, config)
    stage.create("eval-kb", prefix)
    stage.ingest()
    retrieved, expected = [], []
    for r in rows:
        got = [pp.source_id for pp in stage.retrieve(r["question"], top_k=5)]
        hit = any(any(exp in g for g in got) for exp in r["expected_source_ids"])
        retrieved.append(["__HIT__"] if hit else got[:5])
        expected.append("__HIT__" if hit else "__MISS__")
    return {
        "available": True,
        "recall_at_5": round(recall_at_k(retrieved, expected, k=5), 4),
        "note": "fake-mode recall is a smoke-test value, not evidence of the target",
    }


def _score_gaps_and_fp(config, clients) -> dict:
    """Planted gaps (x of 3) + coverage FP rate (approval condition 7).

    Requires an APPROVED alignment. Scores against GROUND TRUTH: groups tickets by
    their seeded intent (from labels.csv, bypassing cluster-naming noise), builds a
    KB from the real documents, runs the coverage check per seeded intent, then:
      * asserts exactly the 3 planted GAP intents are flagged (x of 3), and
      * computes the false-positive rate = documented intents wrongly flagged.
    """
    alignment = REPO_ROOT / "docs" / "intent_alignment.md"
    approved = alignment.exists() and "status: approved" in alignment.read_text().lower()
    if not approved:
        return {
            "available": False,
            "reason": "needs an APPROVED intent/document alignment "
            "(docs/intent_alignment.md status: approved)",
        }

    from kb_agent.documents.knowledge_base import KnowledgeBaseStage
    from kb_agent.documents.staging import is_stageable_document
    from kb_agent.join.coverage import coverage_check
    from kb_agent.models import CoverageStatus, IntentCluster
    from kb_agent.tickets.adapters.sample import SampleAdapter
    from kb_agent.tickets.clean import clean_tickets
    from kb_agent.tickets.loaders import load_tickets_bytes
    from sample_pack.generate_tickets import GAP_INTENTS

    # Build the KB from the real text documents.
    prefix = "evalgaps/kb-docs"
    for p in sorted(DOCS.glob("*")):
        if p.is_file() and is_stageable_document(p.name) and p.suffix != ".pdf":
            clients.store.put_text(f"{prefix}/{p.name}", p.read_text())
    stage = KnowledgeBaseStage(clients.knowledge_base, config)
    stage.create("evalgaps-kb", prefix)
    stage.ingest()

    # Group tickets by SEEDED intent (ground truth), build pseudo-clusters so we
    # score the planted intents directly rather than noisy discovered clusters.
    rows = load_tickets_bytes((PACK / "tickets.csv").read_bytes(), "csv")
    tickets, _ = SampleAdapter().adapt(rows)
    tickets, _ = clean_tickets(tickets)
    truth = _labels_by_id()
    by_intent: dict[str, list] = {}
    for t in tickets:
        by_intent.setdefault(truth[t.ticket_id], []).append(t)

    tickets_by_id = {t.ticket_id: t for t in tickets}
    clusters = [
        IntentCluster(
            cluster_id=i,
            intent_name=intent,
            description=intent.replace("_", " "),
            volume=len(ts),
            resolved_share=1.0,
            example_ticket_ids=[t.ticket_id for t in ts[:3]],
        )
        for i, (intent, ts) in enumerate(sorted(by_intent.items()))
    ]
    # Test ALL intents so we see every documented one (not just top-N by volume).
    cfg = config.model_copy(deep=True)
    cfg.coverage.top_intents_to_test = len(clusters)
    items, _ = coverage_check(clusters, tickets_by_id, stage, clients.llm, cfg)

    flagged = {
        i.intent_name for i in items if i.status in (CoverageStatus.PARTIAL, CoverageStatus.GAP)
    }
    gaps_flagged = sorted(flagged & GAP_INTENTS)
    documented = {i.intent_name for i in items} - GAP_INTENTS
    false_positives = sorted(flagged & documented)
    fp_rate = len(false_positives) / len(documented) if documented else 0.0

    return {
        "available": True,
        "planted_gaps_total": len(GAP_INTENTS),
        "planted_gaps_flagged": len(gaps_flagged),
        "gaps_flagged_names": gaps_flagged,
        "meets_3_of_3": len(gaps_flagged) == len(GAP_INTENTS),
        "coverage_false_positive_rate": round(fp_rate, 4),
        "false_positive_intents": false_positives,
        "note": (
            "Scored against seeded labels + real documents with FAKE lexical "
            "retrieval. The 3-of-3 gap detection is robust (gaps share no "
            "vocabulary with any doc). The FP rate is INFLATED by the fake "
            "retrieval: customer-worded probes share few exact words with the "
            "Nextcloud prose, so on-topic docs score low and get flagged. Expect "
            "the FP rate to fall substantially with real Bedrock embeddings "
            "(--mode aws). Do NOT lower the coverage threshold to mask this - the "
            "FP rate exists to catch exactly that gaming."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scored evaluation run.")
    parser.add_argument("--mode", choices=["fake", "aws"], default="fake")
    args = parser.parse_args()

    settings = get_settings()
    config = load_config()
    clients = build_client_set(args.mode, settings, config)  # type: ignore[arg-type]

    results: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "synthetic_data": True,
        "clustering": _score_clustering(config, clients),
        "routing": _score_routing(config),
        "routing_heldout": _score_routing_heldout(config),
        "pii_document_rejection": _score_pii_document_rejection(config, clients),
        "pii_ticket_rejection": _score_pii_rejection(config, clients),
        "pii_adversarial": _score_pii_adversarial(config, clients),
        "recall": _score_recall(config, clients),
        "gaps_and_fp": _score_gaps_and_fp(config, clients),
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))

    # Print a readable summary + flag anything unavailable (loud, not fake).
    print(f"=== make eval ({args.mode} mode, SYNTHETIC data) ===")
    for section, data in results.items():
        if not isinstance(data, dict):
            continue
        if data.get("available") is False:
            print(f"[UNAVAILABLE] {section}: {data['reason']}")
        elif "available" in data:
            shown = {k: v for k, v in data.items() if k != "available"}
            print(f"[OK] {section}: {json.dumps(shown)}")
    print(f"\nWrote {out}")
    if args.mode == "fake":
        print(
            "NOTE: fake-mode clustering/recall are SMOKE-TEST values (hashed-"
            "bag-of-words stand-in). Do not tune parameters against them; they are "
            "not evidence the targets are met."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
