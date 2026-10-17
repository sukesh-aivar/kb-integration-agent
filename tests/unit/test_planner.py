"""Unit tests for the planner (spec 6: limits + precondition enforcement).

The planner is disabled by default; these tests enable it explicitly to prove the
guardrails. All offline (fake / scripted LLM).
"""

from __future__ import annotations

import json

import pytest

from kb_agent.config import load_config
from kb_agent.exceptions import PlannerLimitError
from kb_agent.planner.planner import Planner
from kb_agent.planner.tools import (
    IngestArgs,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    make_ingest_tool,
)


class ScriptedLlm:
    """LLM stub that returns a fixed sequence of planner decisions."""

    model_id = "scripted"

    def __init__(self, decisions: list[dict]) -> None:
        self._decisions = decisions
        self._i = 0

    def invoke(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0) -> str:
        d = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return json.dumps(d)


def _config(enabled: bool = True, max_steps: int = 20, max_retune: int = 2):
    cfg = load_config()
    cfg.planner.enabled = enabled
    cfg.planner.max_planner_steps = max_steps
    cfg.planner.max_retune_attempts_per_stage = max_retune
    return cfg


# --- default disabled --------------------------------------------------------


def test_planner_is_disabled_by_default() -> None:
    cfg = load_config()
    assert cfg.planner.enabled is False  # OFF pending Q12


def test_disabled_planner_does_nothing() -> None:
    planner = Planner(ScriptedLlm([{"action": "stop"}]), ToolRegistry(), _config(enabled=False))
    outcome = planner.run({})
    assert outcome.stopped_reason == "planner_disabled"
    assert outcome.decisions == []


# --- allowlist ---------------------------------------------------------------


def test_disallowed_tool_is_not_called() -> None:
    registry = ToolRegistry()  # empty allowlist
    llm = ScriptedLlm([{"action": "delete_everything", "reason": "no"}, {"action": "stop"}])
    outcome = Planner(llm, registry, _config()).run({})
    # The disallowed action is recorded as not_allowed, then the planner stops.
    assert any(d.result_status == "not_allowed" for d in outcome.decisions)
    assert outcome.stopped_reason


# --- precondition enforcement (the key spec-6 rule) --------------------------


def _ingest_registry(validation_ok: bool) -> ToolRegistry:
    registry = ToolRegistry()
    spec = ToolSpec(
        name="kb_ingest",
        description="Ingest the KB. Precondition: validation must have passed.",
        args_schema=IngestArgs,
    )
    registry.register(
        spec,
        make_ingest_tool(
            validation_passed=lambda: validation_ok,
            do_ingest=lambda kb_id: f"job-for-{kb_id}",
        ),
    )
    return registry


def test_ingest_refused_when_validation_failed() -> None:
    # Spec 6: kb_ingest refuses to run on failed validation, whatever the plan says.
    registry = _ingest_registry(validation_ok=False)
    llm = ScriptedLlm(
        [
            {"action": "kb_ingest", "args": {"kb_id": "kb1"}, "reason": "try ingest"},
            {"action": "stop"},
        ]
    )
    outcome = Planner(llm, registry, _config()).run({})
    blocked = [d for d in outcome.decisions if d.result_status == "blocked"]
    assert blocked, "ingest should have been blocked by its precondition"


def test_ingest_allowed_when_validation_passed() -> None:
    registry = _ingest_registry(validation_ok=True)
    llm = ScriptedLlm(
        [{"action": "kb_ingest", "args": {"kb_id": "kb1"}, "reason": "ingest"}, {"action": "stop"}]
    )
    outcome = Planner(llm, registry, _config()).run({})
    ok = [d for d in outcome.decisions if d.result_status == "ok"]
    assert ok


def test_tool_rejects_invalid_args_without_calling_fn() -> None:
    registry = _ingest_registry(validation_ok=True)
    # Missing required kb_id -> schema validation fails, fn never runs.
    result = registry.call("kb_ingest", {})
    assert result.status == "error"
    assert "invalid args" in result.message


# --- limits ------------------------------------------------------------------


def test_step_limit_escalates() -> None:
    # An LLM that never stops -> must hit the step limit and raise/escalate.
    registry = ToolRegistry()
    llm = ScriptedLlm([{"action": "noop", "reason": "loop"}])
    with pytest.raises(PlannerLimitError):
        Planner(llm, registry, _config(max_steps=3)).run({})


def test_retune_limit_escalates_without_raising() -> None:
    registry = ToolRegistry()
    # Register a retune tool that always "succeeds" so only the limit stops it.
    from pydantic import BaseModel

    class Args(BaseModel):
        pass

    registry.register(
        ToolSpec(name="retune_clustering", description="retune", args_schema=Args),
        lambda a: ToolResult(status="ok"),
    )
    llm = ScriptedLlm([{"action": "retune_clustering", "reason": "again"}])
    outcome = Planner(llm, registry, _config(max_steps=10, max_retune=2)).run({})
    assert outcome.escalated
    assert "retune limit" in outcome.stopped_reason


def test_every_decision_is_logged() -> None:
    registry = ToolRegistry()
    llm = ScriptedLlm(
        [{"action": "noop", "reason": "r1"}, {"action": "stop", "reason": "done"}]
    )
    outcome = Planner(llm, registry, _config()).run({})
    assert len(outcome.decisions) == 2
    assert outcome.decisions[0].reason == "r1"
    assert outcome.decisions[-1].action == "stop"
