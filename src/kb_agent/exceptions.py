"""Custom exception hierarchy (spec 7.4 error handling).

A single root makes it easy to catch "anything this agent raised" at the
pipeline boundary while still allowing specific handling per failure mode.
"""

from __future__ import annotations


class KBAgentError(Exception):
    """Base class for all errors raised by the KB Integration Agent."""


class ConfigError(KBAgentError):
    """Configuration is missing or malformed."""


class ValidationError(KBAgentError):
    """An input file or ticket failed validation."""


class ParsingError(KBAgentError):
    """A document could not be parsed/extracted."""


class RoutingError(KBAgentError):
    """The router could not evaluate a rule or produce a decision."""


class IngestionError(KBAgentError):
    """Knowledge base creation or ingestion failed."""


class BudgetExceededError(KBAgentError):
    """A per-run cost or page budget was exceeded (spec 4.2 cost guards)."""


class PageCapExceededError(KBAgentError):
    """Documents routed to BDA exceed the per-run page cap, checked BEFORE any
    paid BDA call (spec v2 §5.8). Maps to error kind ``page_cap_exceeded``."""

    def __init__(self, needed: int, cap: int) -> None:
        self.needed = needed
        self.cap = cap
        super().__init__(
            f"The documents need about {needed} pages of scanned parsing; "
            f"the limit for this demo is {cap}."
        )


class PlannerLimitError(KBAgentError):
    """The planner hit a step / retune / timeout limit (spec 6)."""
