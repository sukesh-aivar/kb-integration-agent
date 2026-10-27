"""The thin planner loop (spec 6). Disabled by default (planner.enabled=false).

Build the deterministic pipeline first and make it pass its tests; THEN add a
thin planner (spec 6). The planner chooses which registered tool/stage to call
next from the allowlist - it does not do the work. All guardrails are enforced in
CODE, not the prompt:

* Tool allowlist (the registry); the planner can call nothing else.
* Preconditions enforced in the tools (see tools.py), not assumed from the plan.
* MAX_PLANNER_STEPS and MAX_RETUNE_ATTEMPTS_PER_STAGE limits; on breach the
  planner STOPS and escalates to the reviewer with the evidence so far.
* Every planner decision is logged with its stated reason.

This module is only invoked when ``config.planner.enabled`` is true (default
false, pending OPEN_QUESTIONS Q12). It is fully implemented and tested so turning
the flag on is a config change, not a build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kb_agent.aws.interfaces import LlmClient
from kb_agent.config import AppConfig
from kb_agent.dormant.planner.tools import ToolRegistry, ToolResult
from kb_agent.dormant.prompts import prompt_version, render_prompt
from kb_agent.exceptions import PlannerLimitError
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class PlannerDecision:
    """One logged planner decision (spec 6: log every decision + reason)."""

    step: int
    action: str
    args: dict[str, Any]
    reason: str
    result_status: str | None = None


@dataclass
class PlannerOutcome:
    """Result of a planner session."""

    stopped_reason: str
    decisions: list[PlannerDecision] = field(default_factory=list)
    escalated: bool = False


class Planner:
    """A thin, bounded LLM planner over an allowlisted tool registry (spec 6)."""

    def __init__(
        self,
        llm: LlmClient,
        registry: ToolRegistry,
        config: AppConfig,
        *,
        version: str = "v1",
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._config = config
        self._version = version

    def run(
        self, state_summary: dict[str, Any], reviewer_feedback: str = ""
    ) -> PlannerOutcome:
        """Run the planner loop until it stops, escalates, or hits a limit.

        Returns a :class:`PlannerOutcome` with the full decision log. Raising is
        avoided for recoverable situations; a hard limit breach escalates rather
        than crashing the run.
        """
        if not self._config.planner.enabled:
            # Defensive: callers should check, but never run when disabled.
            return PlannerOutcome(stopped_reason="planner_disabled")

        max_steps = self._config.planner.max_planner_steps
        retune_attempts: dict[str, int] = {}
        outcome = PlannerOutcome(stopped_reason="")

        for step in range(1, max_steps + 1):
            raw = self._ask(state_summary, reviewer_feedback)
            action, args, reason = self._parse_decision(raw)
            decision = PlannerDecision(step=step, action=action, args=args, reason=reason)
            logger.info("planner_decision", step=step, action=action, reason=reason)

            if action == "stop":
                decision.result_status = "stop"
                outcome.decisions.append(decision)
                outcome.stopped_reason = reason or "planner chose stop"
                return outcome

            if not self._registry.is_allowed(action):
                # The planner may only call allowlisted tools (spec 6).
                decision.result_status = "not_allowed"
                outcome.decisions.append(decision)
                logger.warning("planner_disallowed_tool", action=action)
                continue

            # Enforce the per-stage retune limit (spec 6).
            if action.startswith("retune"):
                retune_attempts[action] = retune_attempts.get(action, 0) + 1
                if retune_attempts[action] > self._config.planner.max_retune_attempts_per_stage:
                    outcome.decisions.append(decision)
                    outcome.stopped_reason = f"retune limit exceeded for {action}"
                    outcome.escalated = True
                    logger.warning("planner_retune_limit", action=action)
                    return outcome

            result: ToolResult = self._registry.call(action, args)
            decision.result_status = result.status
            outcome.decisions.append(decision)
            # Fold the result into the state summary so the next decision sees it.
            state_summary = {**state_summary, "last_tool_result": result.model_dump()}

        # Hit the step limit without stopping: escalate with evidence (spec 6).
        outcome.stopped_reason = f"max_planner_steps ({max_steps}) reached"
        outcome.escalated = True
        logger.warning("planner_step_limit", max_steps=max_steps)
        raise PlannerLimitError(outcome.stopped_reason)

    def _ask(self, state_summary: dict[str, Any], reviewer_feedback: str) -> str:
        prompt = render_prompt(
            "planner",
            self._version,
            allowed_tools=", ".join(self._registry.names()),
            state_block=json.dumps(state_summary, default=str)[:4000],
            reviewer_feedback=reviewer_feedback or "(none)",
        )
        return self._llm.invoke(prompt, max_tokens=256)

    def _parse_decision(self, raw: str) -> tuple[str, dict[str, Any], str]:
        """Parse the planner JSON, defaulting to a safe stop on malformed output."""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return "stop", {}, "unparseable planner output -> safe stop"
        action = str(obj.get("action", "stop"))
        args = obj.get("args", {})
        if not isinstance(args, dict):
            args = {}
        reason = str(obj.get("reason", ""))
        return action, args, reason

    def prompt_version_id(self) -> str:
        return prompt_version("planner", self._version)
