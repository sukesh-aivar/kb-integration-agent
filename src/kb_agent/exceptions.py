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


# NOTE (Part B, decision 4): the agent imposes NO page or cost caps. The former
# BudgetExceededError / PageCapExceededError were removed - the only ceiling is
# BDA's own native per-document page limit (surfaced as a REJECT with a reason).


class PlannerLimitError(KBAgentError):
    """The planner hit a step / retune / timeout limit (spec 6)."""
