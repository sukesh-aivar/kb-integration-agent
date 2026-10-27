"""draft_article: synthesise resolved tickets into cited articles (spec 4.4).

Drafting is permitted LLM use #3. The hard rules, all enforced in CODE (not
prompts):

* Only intents with >= min_source_tickets RESOLVED tickets are drafted
  (default 5, TODO(confirm) Q3). One ticket is a one-off; several give the model
  enough evidence to find the consistent resolution.
* Volume is NOT a prompt input - frequency is used by code to decide WHICH intents
  to draft, not by the model when writing (spec 4.4).
* Citations are mandatory and VALIDATED: every cited ticket_id must exist and
  belong to this intent, or the draft is rejected.
* A PII re-scan (Guardrails + regex) runs on the generated text; a draft failing
  the scan is blocked and flagged, never stored as approvable.
* Drafts are written ONLY to ``drafts/{run_id}/`` - a prefix the KB data source
  cannot read (spec 2.2). There is no path from here into the index.
"""

from __future__ import annotations

import json

from kb_agent.aws.interfaces import GuardrailClient, LlmClient, ObjectStore
from kb_agent.config import AppConfig
from kb_agent.documents.pii import regex_pii_findings
from kb_agent.dormant.llm_json import extract_json
from kb_agent.dormant.prompts import prompt_version, render_prompt
from kb_agent.logging_setup import get_logger
from kb_agent.models import (
    Article,
    CoverageItem,
    CoverageStatus,
    IntentCluster,
    ReviewStatus,
    Ticket,
)

logger = get_logger(__name__)

DRAFTS_PREFIX = "drafts"  # drafts/{run_id}/... - NEVER a KB data-source prefix


class DraftOutcome:
    """Result of attempting to draft one intent's article."""

    def __init__(
        self,
        intent_name: str,
        article: Article | None,
        drafted: bool,
        reason: str,
    ) -> None:
        self.intent_name = intent_name
        self.article = article
        self.drafted = drafted
        self.reason = reason


def _resolved_tickets_for_intent(
    cluster: IntentCluster,
    labels: list[int],
    tickets: list[Ticket],
) -> list[Ticket]:
    """All RESOLVED tickets whose cluster label == this intent's cluster_id."""
    out: list[Ticket] = []
    for idx, label in enumerate(labels):
        if label == cluster.cluster_id and tickets[idx].resolution:
            out.append(tickets[idx])
    return out


def draft_articles(
    clusters: list[IntentCluster],
    labels: list[int],
    tickets: list[Ticket],
    coverage: list[CoverageItem],
    *,
    run_id: str,
    llm: LlmClient,
    guardrail: GuardrailClient,
    store: ObjectStore,
    config: AppConfig,
    version: str = "v1",
) -> tuple[list[DraftOutcome], str]:
    """Draft an article only for coverage GAPS, not every intent (spec 4.4).

    An article is drafted for an intent only when BOTH hold:
      1. the coverage check marked it ``partial`` or ``gap`` (the KB does not
         already answer it) - drafting to fill a gap the KB already covers is
         wasted work and risks duplicating existing content; and
      2. it has at least ``min_source_tickets`` RESOLVED tickets - enough evidence
         to synthesise a consistent resolution (spec 4.4).

    ``covered`` intents and intents the coverage check did not test are skipped
    with a recorded reason. Returns ``(outcomes, prompt_version_id)``; every
    outcome records whether an article was produced and, if not, why, so the
    report can account for all of them.
    """
    min_tickets = config.drafting.min_source_tickets
    # Intents the coverage check flagged as not (fully) answered by the KB.
    gap_intents = {
        item.intent_name
        for item in coverage
        if item.status in (CoverageStatus.PARTIAL, CoverageStatus.GAP)
    }
    tested_intents = {item.intent_name for item in coverage}
    outcomes: list[DraftOutcome] = []

    for cluster in clusters:
        if cluster.cluster_id == -1:
            continue

        # Gate 1: only draft for coverage gaps/partials (spec 4.4).
        if cluster.intent_name not in gap_intents:
            if cluster.intent_name in tested_intents:
                reason = "covered by KB (not a gap)"
            else:
                reason = "not among coverage-tested intents"
            outcomes.append(DraftOutcome(cluster.intent_name, None, False, reason))
            continue

        resolved = _resolved_tickets_for_intent(cluster, labels, tickets)

        # Gate 2: enough resolved tickets to synthesise from (spec 4.4).
        if len(resolved) < min_tickets:
            outcomes.append(
                DraftOutcome(
                    cluster.intent_name,
                    None,
                    False,
                    f"gap, but only {len(resolved)} resolved tickets (<{min_tickets})",
                )
            )
            continue

        valid_ids = {t.ticket_id for t in resolved}
        tickets_block = "\n\n".join(
            f"[{t.ticket_id}] problem: {t.embedding_text()}\nresolution: {t.resolution}"
            for t in resolved[: 2 * min_tickets]  # cap the prompt size
        )
        prompt = render_prompt(
            "drafting",
            version,
            intent_name=cluster.intent_name,
            intent_description=cluster.description,
            tickets_block=tickets_block,
        )
        # 2048 tokens: the article JSON plus an internal reasoning block the
        # newer models emit; 1024 truncated mid-JSON ("unparseable draft JSON").
        raw = llm.invoke(prompt, max_tokens=2048)
        outcome = _finalise_draft(
            raw, cluster, valid_ids, run_id, llm.model_id, guardrail, store, version
        )
        outcomes.append(outcome)

    drafted = sum(1 for o in outcomes if o.drafted)
    logger.info("drafting_complete", intents=len(outcomes), drafted=drafted)
    return outcomes, prompt_version("drafting", version)


def _finalise_draft(
    raw: str,
    cluster: IntentCluster,
    valid_ids: set[str],
    run_id: str,
    model_id: str,
    guardrail: GuardrailClient,
    store: ObjectStore,
    version: str,
) -> DraftOutcome:
    """Parse, validate citations, PII re-scan, and store one draft (spec 4.4)."""
    try:
        obj = extract_json(raw)
    except json.JSONDecodeError:
        return DraftOutcome(cluster.intent_name, None, False, "unparseable draft JSON")

    cited = [str(c) for c in obj.get("source_ticket_ids", [])]

    # Citation validation: every cited id must exist AND belong to this intent.
    if not cited:
        return DraftOutcome(cluster.intent_name, None, False, "no citations")
    bad = [c for c in cited if c not in valid_ids]
    if bad:
        return DraftOutcome(
            cluster.intent_name, None, False, f"citations not in intent: {bad[:5]}"
        )

    # PII re-scan on the GENERATED text (Guardrails + regex), in code (spec 4.4).
    generated_text = " ".join(
        [
            str(obj.get("title", "")),
            str(obj.get("problem", "")),
            str(obj.get("applies_to", "")),
            " ".join(str(s) for s in obj.get("steps", [])),
            str(obj.get("resolution", "")),
        ]
    )
    pii_hits = list(regex_pii_findings(generated_text)) + list(guardrail.scan_pii(generated_text))
    pii_passed = not pii_hits

    article = Article(
        article_id=f"{run_id}:{cluster.intent_name}",
        intent_name=cluster.intent_name,
        title=str(obj.get("title", cluster.intent_name)),
        problem=str(obj.get("problem", "")),
        applies_to=str(obj.get("applies_to", "")),
        steps=[str(s) for s in obj.get("steps", [])],
        resolution=str(obj.get("resolution", "")),
        source_ticket_ids=cited,
        model_id=model_id,
        prompt_version=prompt_version("drafting", version),
        pii_scan_passed=pii_passed,
        status=ReviewStatus.PENDING_REVIEW,
    )

    if not pii_passed:
        # Blocked: flagged, never stored as approvable. We DO record it (for the
        # report) but do not write it to the drafts prefix as an approvable file.
        logger.warning("draft_pii_blocked", intent=cluster.intent_name, hits=len(pii_hits))
        return DraftOutcome(
            cluster.intent_name, article, False, f"blocked: PII in draft ({len(pii_hits)} hits)"
        )

    # Write ONLY under drafts/{run_id}/ - a prefix the KB cannot read (spec 2.2).
    key = f"{DRAFTS_PREFIX}/{run_id}/{cluster.intent_name}.json"
    store.put_text(key, article.model_dump_json(indent=2))
    return DraftOutcome(cluster.intent_name, article, True, f"stored at {key}")
