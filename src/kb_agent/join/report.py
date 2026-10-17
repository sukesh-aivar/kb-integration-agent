"""write_report: the reviewable output package (spec 4.4).

One package containing KB status + recall baseline, the intent view with volumes,
cited draft articles, coverage gaps, run metrics, and handling time per stage -
plus the EXPLICIT note that all metrics are measured on synthetic data (spec 1.5,
4.4). Emitted as both Markdown (for humans) and JSON (for machines), both marked
``pending_review``.
"""

from __future__ import annotations

from kb_agent.contract import RunMetrics
from kb_agent.join.drafting import DraftOutcome
from kb_agent.models import CoverageItem, IntentCluster

SYNTHETIC_NOTE = (
    "All metrics in this report are measured on SYNTHETIC data. They validate the "
    "pipeline, not real-world quality."
)

# Caveats shown in the report when the run used fake clients (spec review items
# 5 and 6): fake-mode clustering/recall numbers are smoke-test values only and
# must NOT be treated as evidence that the validation targets are met, nor used to
# tune clustering parameters.
FAKE_MODE_CAVEATS = [
    "This run used FAKE clients (in-memory stand-ins for S3/BDA/KB/embeddings/LLM).",
    "Intent purity, cluster count and silhouette here are SMOKE-TEST values from a "
    "hashed-bag-of-words stand-in, not a semantic embedding model. Do NOT tune "
    "clustering parameters against them.",
    "Fake-mode recall@5 is NOT evidence that the >=0.80 retrieval target is met. "
    "Real recall requires Bedrock embeddings (--mode aws) and a human-approved "
    "golden set; the committed golden_set.jsonl still contains placeholder rows.",
]


def build_report(
    *,
    run_id: str,
    kb_id: str | None,
    metrics: RunMetrics,
    clusters: list[IntentCluster],
    coverage: list[CoverageItem],
    drafts: list[DraftOutcome],
    mode: str = "fake",
    expected_intent_count: int = 20,
) -> dict:
    """Assemble the machine-readable (JSON) report payload.

    Args:
        mode: run mode ("fake" | "aws"). In fake mode the report carries explicit
            smoke-test caveats (spec review items 5, 6).
        expected_intent_count: the number of planted intents, so cluster count is
            reported against its target (spec 1.5 "cluster count vs 20").
    """
    return {
        "run_id": run_id,
        "synthetic_data_note": SYNTHETIC_NOTE,
        "mode": mode,
        "caveats": FAKE_MODE_CAVEATS if mode == "fake" else [],
        "targets": {
            "intent_purity": 0.80,
            "recall_at_5": 0.80,
            "expected_intent_count": expected_intent_count,
            "pii_files_rejected": 1,
            "planted_gaps": 3,
        },
        "status": "pending_review",
        "knowledge_base": {"kb_id": kb_id, "recall_at_5": metrics.recall_at_5},
        "metrics": metrics.model_dump(mode="json"),
        "intent_view": [
            {
                "intent_name": c.intent_name,
                "description": c.description,
                "volume": c.volume,
                "resolved_share": c.resolved_share,
                "example_ticket_ids": c.example_ticket_ids,
                "coherence_flag": c.coherence_flag,
                "is_noise": c.cluster_id == -1,
            }
            for c in clusters
        ],
        "coverage": [
            {
                "intent_name": i.intent_name,
                "status": i.status.value,
                "volume": i.volume,
                "priority": i.priority,
                "reason": i.reason,
            }
            for i in coverage
        ],
        "draft_articles": [
            {
                "intent_name": o.intent_name,
                "drafted": o.drafted,
                "reason": o.reason,
                "title": o.article.title if o.article else None,
                "citations": o.article.source_ticket_ids if o.article else [],
                "pii_scan_passed": o.article.pii_scan_passed if o.article else None,
            }
            for o in drafts
        ],
        "drafting_summary": {
            "intents_considered": len(drafts),
            "drafted": sum(1 for o in drafts if o.drafted),
            "skipped": sum(1 for o in drafts if not o.drafted),
            "skip_reasons": _skip_reason_counts(drafts),
        },
    }


def _skip_reason_counts(drafts: list[DraftOutcome]) -> dict[str, int]:
    """Count skipped drafts by a normalised reason category (spec review item 8)."""
    counts: dict[str, int] = {}
    for o in drafts:
        if o.drafted:
            continue
        reason = o.reason
        if "not a gap" in reason or "covered" in reason:
            key = "covered_by_kb"
        elif "not among coverage-tested" in reason:
            key = "not_coverage_tested"
        elif "resolved tickets" in reason:
            key = "too_few_resolved_tickets"
        elif "citations" in reason:
            key = "citation_validation_failed"
        elif "PII" in reason:
            key = "blocked_pii_in_draft"
        else:
            key = "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def render_markdown(report: dict) -> str:
    """Render the report as Markdown for human reviewers."""
    lines: list[str] = []
    lines.append(f"# KB Integration Agent - Run Report `{report['run_id']}`")
    lines.append("")
    lines.append(f"> **{report['synthetic_data_note']}**")
    lines.append("")
    lines.append(f"Status: **{report['status']}**  ·  Mode: **{report.get('mode', 'fake')}**")
    lines.append("")

    caveats = report.get("caveats") or []
    if caveats:
        lines.append("## ⚠️ Caveats (fake-mode run)")
        lines.append("")
        for c in caveats:
            lines.append(f"- {c}")
        lines.append("")

    kb = report["knowledge_base"]
    m = report["metrics"]
    targets = report.get("targets", {})
    lines.append("## Knowledge base")
    lines.append(f"- KB id: `{kb['kb_id']}`")
    recall_target = targets.get("recall_at_5")
    recall_suffix = f" (target >= {recall_target})" if recall_target else ""
    lines.append(f"- recall@5: {_fmt(kb['recall_at_5'])}{recall_suffix}")
    if report.get("mode") == "fake":
        lines.append(
            "  - NOTE: fake-mode recall is a smoke-test value, NOT evidence the "
            "target is met; the golden set needs human approval."
        )
    lines.append("")

    # Cluster count is shown against its target (spec 1.5 "cluster count vs 20").
    expected = targets.get("expected_intent_count")
    cluster_count = m.get("cluster_count")
    cluster_line = _fmt(cluster_count)
    if expected is not None and cluster_count is not None:
        delta = cluster_count - expected
        cluster_line = f"{cluster_count} vs {expected} planted (delta {delta:+d})"

    lines.append("## Run metrics")
    lines.append(f"- Intent purity: {_fmt(m.get('intent_purity'))} (target >= 0.80)")
    lines.append(f"- Silhouette: {_fmt(m.get('silhouette'))}")
    lines.append(f"- Cluster count: {cluster_line}")
    lines.append(f"- Noise share: {_fmt(m.get('noise_share'))}")
    lines.append(f"- PII files rejected: {_fmt(m.get('pii_files_rejected'))} (target 1 of 1)")
    lines.append(f"- Gaps flagged: {_fmt(m.get('gaps_flagged'))}")
    lines.append(f"- Coverage FP rate: {_fmt(m.get('coverage_fp_rate'))}")
    lines.append(f"- Routing accuracy: {_fmt(m.get('routing_accuracy'))}")
    lines.append(f"- Tickets: {_fmt(m.get('ticket_count'))}")
    lines.append(f"- Documents: {_fmt(m.get('document_count'))}")
    if m.get("stage_durations_sec"):
        lines.append("")
        lines.append("### Handling time per stage (seconds)")
        for stage, secs in m["stage_durations_sec"].items():
            lines.append(f"- {stage}: {secs:.3f}")
    lines.append("")

    lines.append("## Intent view")
    lines.append("")
    lines.append("| Intent | Volume | Resolved share | Coherence flag |")
    lines.append("|---|---|---|---|")
    for c in report["intent_view"]:
        flag = "mixed" if c["coherence_flag"] else ""
        name = f"{c['intent_name']} (noise)" if c["is_noise"] else c["intent_name"]
        lines.append(f"| {name} | {c['volume']} | {c['resolved_share']:.2f} | {flag} |")
    lines.append("")

    lines.append("## Coverage gaps (by priority)")
    lines.append("")
    lines.append("| Intent | Status | Volume | Priority |")
    lines.append("|---|---|---|---|")
    for i in report["coverage"]:
        lines.append(f"| {i['intent_name']} | {i['status']} | {i['volume']} | {i['priority']} |")
    lines.append("")

    lines.append("## Draft articles")
    lines.append("")
    summary = report.get("drafting_summary", {})
    if summary:
        reasons = ", ".join(f"{k}={v}" for k, v in summary.get("skip_reasons", {}).items())
        lines.append(
            f"**{summary.get('drafted', 0)} drafted / {summary.get('skipped', 0)} skipped "
            f"of {summary.get('intents_considered', 0)} intents considered.** "
            f"Skip reasons: {reasons or 'none'}."
        )
        lines.append("")
    lines.append("| Intent | Drafted? | Title / skip reason |")
    lines.append("|---|---|---|")
    for d in report["draft_articles"]:
        if d["drafted"]:
            cites = ", ".join(d["citations"])
            lines.append(f"| {d['intent_name']} | yes | {d['title']} (sources: {cites}) |")
        else:
            lines.append(f"| {d['intent_name']} | no | {d['reason']} |")
    lines.append("")
    return "\n".join(lines)


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
