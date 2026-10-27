"""clean_tickets: strip boilerplate, dedupe, build embedding text (spec 4.3).

Key spec rule enforced here: the text to embed is **subject + description only**
- the customer's problem. The resolution and metadata are deliberately NOT
embedded, because intent is what the customer asked for; including resolutions
pulls clusters toward "how it was fixed", a different partition of the data.

Also enforces the ground-truth separation at the embedding boundary: the only
text that ever leaves this stage toward embeddings/prompts comes from
``Ticket.embedding_text()``, which is subject+description. A dedicated leak test
(test_ground_truth_leak.py) asserts no seeded label string appears in it.
"""

from __future__ import annotations

import re

from kb_agent.logging_setup import get_logger
from kb_agent.models import Ticket

logger = get_logger(__name__)

# Patterns for common boilerplate to strip from ticket bodies before embedding.
_SIGNATURE_RE = re.compile(
    r"(?is)(?:^|\n)\s*(?:--\s*\n.*|regards[,.]?\s.*|best[,.]?\s.*|sent from my \w+.*)$"
)
_QUOTED_REPLY_RE = re.compile(r"(?im)^\s*>.*$")
_ON_WROTE_RE = re.compile(r"(?is)\n*on .{0,80}? wrote:.*$")
_DISCLAIMER_RE = re.compile(
    r"(?is)\n*(?:this (?:e-?mail|message) (?:and any attachments|is confidential).*)$"
)
_WS_RE = re.compile(r"[ \t]+")


def clean_text(text: str) -> str:
    """Strip signatures, quoted reply chains, and disclaimers from a body."""
    if not text:
        return ""
    text = _ON_WROTE_RE.sub("", text)
    text = _QUOTED_REPLY_RE.sub("", text)
    text = _DISCLAIMER_RE.sub("", text)
    text = _SIGNATURE_RE.sub("", text)
    # Collapse runs of whitespace but keep paragraph breaks.
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_tickets(tickets: list[Ticket]) -> tuple[list[Ticket], int]:
    """Clean bodies and deduplicate (spec 4.3).

    Deduplication is by cleaned ``subject + description`` so the seeded exact
    duplicates are caught regardless of ticket_id. Returns the cleaned, unique
    tickets and the number of duplicates removed.
    """
    seen: set[str] = set()
    out: list[Ticket] = []
    duplicates = 0

    for ticket in tickets:
        cleaned_subject = clean_text(ticket.subject)
        cleaned_desc = clean_text(ticket.description)
        key = f"{cleaned_subject}\n{cleaned_desc}".lower()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        # Rebuild the ticket with cleaned text; resolution left intact (it is used
        # by drafting, not by embedding).
        out.append(
            ticket.model_copy(update={"subject": cleaned_subject, "description": cleaned_desc})
        )

    logger.info("clean_complete", input=len(tickets), unique=len(out), duplicates=duplicates)
    return out, duplicates


def embedding_texts(tickets: list[Ticket]) -> list[str]:
    """Return the subject+description text to embed, one per ticket (spec 4.3).

    This is the ONLY path from tickets to embeddings. It never includes the
    resolution, tags, status, or any label.
    """
    return [t.embedding_text() for t in tickets]
