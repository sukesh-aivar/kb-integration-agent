"""coverage_check: does the KB answer the frequent intents? (spec 4.4).

For the top intents by volume, build probe questions from the ACTUAL customer
wording of each cluster's central tickets (not LLM-invented questions where real
ones exist), retrieve from the KB, then judge covered | partial | gap using BOTH
a retrieval-score threshold AND an LLM judge (permitted LLM use #4). Score alone
is insufficient: retrieval routinely returns passages that are on-topic but do not
answer (spec 4.4).

Priority = volume x gap, so a big undocumented intent sorts above a small one.
The false-positive rate (covered intents wrongly flagged) is reported separately
because "3 of 3 gaps flagged" is trivially gamed by flagging everything (spec 1.5).
"""

from __future__ import annotations

import json

from kb_agent.aws.interfaces import LlmClient
from kb_agent.config import AppConfig
from kb_agent.documents.knowledge_base import KnowledgeBaseStage
from kb_agent.logging_setup import get_logger
from kb_agent.models import CoverageItem, CoverageStatus, IntentCluster, Ticket
from kb_agent.prompts import prompt_version, render_prompt

logger = get_logger(__name__)


def build_probe_questions(
    cluster: IntentCluster, tickets_by_id: dict[str, Ticket], max_probes: int = 3
) -> list[str]:
    """Build probe questions from the cluster's example tickets' real wording.

    Uses the customer's actual subject/description text rather than inventing
    questions, per spec 4.4.
    """
    probes: list[str] = []
    for tid in cluster.example_ticket_ids[:max_probes]:
        ticket = tickets_by_id.get(tid)
        if ticket:
            probes.append(ticket.embedding_text())
    # Fall back to the intent name only if the cluster had no example tickets.
    return probes or [cluster.intent_name.replace("_", " ")]


def _judge(
    question: str, passages: list, llm: LlmClient, version: str
) -> tuple[CoverageStatus, str]:
    """LLM judge: do these passages actually answer the question? (spec 4.4)."""
    passages_block = "\n\n".join(f"PASSAGE: {p.text}" for p in passages) or "(no passages)"
    prompt = render_prompt(
        "coverage", version, question=question, passages_block=passages_block
    )
    raw = llm.invoke(prompt, max_tokens=128)
    try:
        obj = json.loads(raw)
        status = CoverageStatus(obj.get("status", "gap"))
        reason = str(obj.get("reason", ""))
    except (json.JSONDecodeError, ValueError):
        status, reason = CoverageStatus.GAP, "unparseable judge output"
    return status, reason


def coverage_check(
    clusters: list[IntentCluster],
    tickets_by_id: dict[str, Ticket],
    kb: KnowledgeBaseStage,
    llm: LlmClient,
    config: AppConfig,
    version: str = "v1",
) -> tuple[list[CoverageItem], str]:
    """Run the coverage check over the top intents by volume (spec 4.4).

    Combines a retrieval-score threshold with the LLM judge: an intent is only
    "covered" if the best retrieval score clears the threshold AND the judge says
    covered. Returns ``(items_sorted_by_priority, prompt_version_id)``.
    """
    threshold = config.coverage.retrieval_score_threshold
    top_n = config.coverage.top_intents_to_test
    k = config.knowledge_base.retrieval_top_k

    # Top intents by volume, excluding the noise bucket.
    ranked = sorted(
        (c for c in clusters if c.cluster_id != -1), key=lambda c: c.volume, reverse=True
    )[:top_n]

    items: list[CoverageItem] = []
    for cluster in ranked:
        probes = build_probe_questions(cluster, tickets_by_id)
        best_score = 0.0
        best_passages: list = []
        for probe in probes:
            passages = kb.retrieve(probe, top_k=k)
            if passages and passages[0].score > best_score:
                best_score = passages[0].score
                best_passages = passages

        judged, reason = _judge(probes[0], best_passages, llm, version)

        # Combine signals: downgrade to gap/partial if the score is weak even when
        # the judge is generous, because a weak top score means poor retrieval.
        if best_score < threshold and judged == CoverageStatus.COVERED:
            status = CoverageStatus.PARTIAL
            reason = f"{reason} (score {best_score:.2f} below {threshold})"
        else:
            status = judged

        gap_weight = {
            CoverageStatus.COVERED: 0.0,
            CoverageStatus.PARTIAL: 0.5,
            CoverageStatus.GAP: 1.0,
        }[status]
        items.append(
            CoverageItem(
                intent_name=cluster.intent_name,
                status=status,
                volume=cluster.volume,
                evidence_passages=[p.text[:200] for p in best_passages[:2]],
                priority=round(cluster.volume * gap_weight, 2),
                reason=reason,
            )
        )

    items.sort(key=lambda i: i.priority, reverse=True)
    logger.info(
        "coverage_complete",
        tested=len(items),
        gaps=sum(1 for i in items if i.status == CoverageStatus.GAP),
    )
    return items, prompt_version("coverage", version)


def false_positive_rate_from_items(
    items: list[CoverageItem], truly_covered: dict[str, bool]
) -> float:
    """FP rate helper (spec 1.5): covered intents wrongly flagged as gap/partial.

    ``truly_covered`` maps intent_name -> whether the KB really covers it (known
    in evaluation because we control which documents were ingested).

    FP rate = (truly covered AND flagged gap/partial) / (truly covered). Computed
    inline so this runtime module does not import ``evals`` (evals depends on src,
    not the reverse).
    """
    gap_values = {CoverageStatus.GAP, CoverageStatus.PARTIAL}
    covered_total = sum(1 for v in truly_covered.values() if v)
    if covered_total == 0:
        return 0.0
    false_positives = sum(
        1 for i in items if truly_covered.get(i.intent_name, False) and i.status in gap_values
    )
    return false_positives / covered_total
