"""categorise: the <500-ticket LLM fallback path (spec 4.3).

Below the configured threshold, density clustering is unstable, so each ticket is
classified directly by the LLM against a provided intent list. This is permitted
LLM use #2 (spec 2.1). The provided candidate list MUST NOT be the seeded labels
or the purity test is circular (spec 4.3) - the caller supplies candidates and the
ground-truth leak test enforces separation.

Output is parsed and schema-validated; a malformed response falls back to
"uncategorised" rather than crashing the run.
"""

from __future__ import annotations

import json

from kb_agent.aws.interfaces import LlmClient
from kb_agent.dormant.llm_json import extract_json
from kb_agent.dormant.prompts import prompt_version, render_prompt
from kb_agent.logging_setup import get_logger
from kb_agent.models import Ticket

logger = get_logger(__name__)


def categorise_tickets(
    tickets: list[Ticket],
    candidates: list[str],
    llm: LlmClient,
    version: str = "v1",
) -> tuple[list[str], str]:
    """Classify each ticket into one of ``candidates`` (or 'uncategorised').

    Args:
        tickets: The tickets to classify.
        candidates: Allowed intent labels (NOT the seeded labels).
        llm: Injected LLM client.
        version: Prompt version to use/record.

    Returns:
        ``(labels, prompt_version_id)`` where labels aligns 1:1 with tickets.
    """
    allowed = set(candidates) | {"uncategorised"}
    labels: list[str] = []

    for ticket in tickets:
        prompt = render_prompt(
            "categorise",
            version,
            candidates=", ".join(repr(c) for c in candidates),
            ticket_text=ticket.embedding_text(),  # subject+description only
        )
        # 256 tokens: a reasoning block can exceed a tiny cap before the label.
        raw = llm.invoke(prompt, max_tokens=256)
        label = _parse_label(raw, allowed)
        labels.append(label)

    logger.info("categorise_complete", tickets=len(tickets), candidates=len(candidates))
    return labels, prompt_version("categorise", version)


def _parse_label(raw: str, allowed: set[str]) -> str:
    """Parse the LLM JSON and coerce to an allowed label, defaulting safely."""
    try:
        obj = extract_json(raw)
        label = str(obj.get("intent", "uncategorised"))
    except (json.JSONDecodeError, AttributeError):
        return "uncategorised"
    return label if label in allowed else "uncategorised"
