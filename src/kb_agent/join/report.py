"""write_report: the reviewable output package — KB-only workflow (v3).

Emits the run report as JSON (machine) and Markdown (human). In the KB-only
workflow there are no tickets, intents, coverage or drafted articles, so those
sections are present-but-empty for backward compatibility (rule 5: no key is
removed from report.json) and carry ``"workflow": "kb_only"``.

The full ticket/coverage/drafting-aware builder + renderer is preserved verbatim
in ``kb_agent.dormant.join.report_sections`` and is re-wired when those stages
return. This active module deliberately imports NO dormant types.
"""

from __future__ import annotations

from typing import Any

from kb_agent.contract import RunMetrics

SYNTHETIC_NOTE = (
    "All metrics in this report are measured on SYNTHETIC data. They validate the "
    "pipeline, not real-world quality."
)

FAKE_MODE_CAVEATS = [
    "This run used FAKE clients (in-memory stand-ins for S3/BDA/KB/embeddings/LLM).",
    "Fake-mode recall@5 is NOT evidence that the >=0.80 retrieval target is met. "
    "Real recall requires Bedrock embeddings (--mode aws) and a human-approved "
    "golden set; the committed golden_set.jsonl still contains placeholder rows.",
]


def build_report(
    *,
    run_id: str,
    kb_id: str | None,
    metrics: RunMetrics,
    clusters: Any = None,
    coverage: Any = None,
    drafts: Any = None,
    mode: str = "fake",
    expected_intent_count: int = 20,
) -> dict:
    """Assemble the KB-only machine-readable (JSON) report payload.

    ``clusters``/``coverage``/``drafts`` are accepted for call-site compatibility
    but ignored: the KB-only workflow never produces them, so the corresponding
    report sections are emitted empty (rule 5: keys kept, additive-only). The
    full builder that renders them lives in
    ``kb_agent.dormant.join.report_sections``.
    """
    return {
        "run_id": run_id,
        "workflow": "kb_only",
        "synthetic_data_note": SYNTHETIC_NOTE,
        "mode": mode,
        "caveats": FAKE_MODE_CAVEATS if mode == "fake" else [],
        "targets": {
            "recall_at_5": 0.80,
            "expected_intent_count": expected_intent_count,
            "pii_files_rejected": 1,
        },
        "status": "pending_review",
        "knowledge_base": {"kb_id": kb_id, "recall_at_5": metrics.recall_at_5},
        "metrics": metrics.model_dump(mode="json"),
        # Dormant sections — present-but-empty in the KB-only workflow (rule 5).
        "intent_view": [],
        "coverage": [],
        "draft_articles": [],
        "drafting_summary": {},
    }


def render_markdown(report: dict) -> str:
    """Render the KB-only report as Markdown (drops ticket/coverage/draft sections)."""
    lines: list[str] = []
    lines.append(f"# KB Integration Agent - Run Report `{report['run_id']}`")
    lines.append("")
    lines.append(f"> **{report['synthetic_data_note']}**")
    lines.append("")
    lines.append(
        f"Status: **{report['status']}**  ·  Mode: **{report.get('mode', 'fake')}**  ·  "
        f"Workflow: **{report.get('workflow', 'kb_only')}**"
    )
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
    lines.append("")

    lines.append("## Run metrics")
    lines.append(f"- PII files rejected: {_fmt(m.get('pii_files_rejected'))} (target 1 of 1)")
    lines.append(f"- Routing accuracy: {_fmt(m.get('routing_accuracy'))}")
    lines.append(f"- Documents: {_fmt(m.get('document_count'))}")
    if m.get("stage_durations_sec"):
        lines.append("")
        lines.append("### Handling time per stage (seconds)")
        for stage, secs in m["stage_durations_sec"].items():
            lines.append(f"- {stage}: {secs:.3f}")
    lines.append("")
    return "\n".join(lines)


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
