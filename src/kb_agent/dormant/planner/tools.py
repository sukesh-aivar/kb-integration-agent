"""Planner tool registry: specs + functions with preconditions (spec 6).

The planner CHOOSES which registered tool to call next; it does not do the work.
These tools are the only things it may call (allowlist). Critically, PRECONDITIONS
ARE ENFORCED IN THE TOOL, not assumed from the plan (spec 6): e.g. a tool that
would ingest refuses to run on a file whose validation did not pass, regardless
of what the planner asked for.

Each tool validates its arguments against a Pydantic schema BEFORE execution; on
failure the function is never called and a structured ``status: "error"`` result
is returned for the planner to recover from (spec 6), rather than raising.

Tool docstrings ARE the prompt the model reads (spec 6): each says when to use it
and when not to.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


class ToolResult(BaseModel):
    """Structured, concise tool result (facts/metrics, not giant payloads)."""

    status: str  # "ok" | "error" | "blocked"
    data: dict[str, Any] = {}
    message: str = ""


class ToolSpec(BaseModel):
    """A registered tool: name, when-to-use doc, and its argument schema."""

    name: str
    description: str
    args_schema: type[BaseModel]

    model_config = {"arbitrary_types_allowed": True}


class PreconditionError(Exception):
    """Raised inside a tool when a precondition is not met (spec 6)."""


class ToolRegistry:
    """Allowlist of tools the planner may call (spec 6).

    The planner can call nothing that is not registered here. Registration binds
    a spec (for the prompt) to a function (the implementation). Arguments are
    schema-validated before the function runs.
    """

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[[BaseModel], ToolResult]]] = {}

    def register(
        self, spec: ToolSpec, fn: Callable[[BaseModel], ToolResult]
    ) -> None:
        self._tools[spec.name] = (spec, fn)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name][0]

    def is_allowed(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, raw_args: dict[str, Any]) -> ToolResult:
        """Validate args against the schema, then run the tool (spec 6).

        A tool not on the allowlist, or args that fail validation, return an
        actionable error result - the function is never called in those cases.
        """
        if name not in self._tools:
            return ToolResult(status="error", message=f"tool not allowed: {name}")
        spec, fn = self._tools[name]
        try:
            args = spec.args_schema.model_validate(raw_args)
        except ValidationError as exc:
            return ToolResult(status="error", message=f"invalid args for {name}: {exc}")
        try:
            return fn(args)
        except PreconditionError as exc:
            logger.warning("tool_precondition_failed", tool=name, reason=str(exc))
            return ToolResult(status="blocked", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface as recoverable error
            logger.error("tool_error", tool=name, error=str(exc))
            return ToolResult(status="error", message=str(exc))


# --- Example tool arg schemas + precondition demonstrations ------------------
# These model the real decisions the planner is allowed to make (spec 6 table:
# retune/re-run, skip inapplicable, respond to failure). The pipeline injects the
# concrete callables; here we define schemas and a precondition-enforcing wrapper
# factory so the enforcement lives in the tool, not the plan.


class RetuneClusteringArgs(BaseModel):
    """Args for retuning clustering (planner may request this on poor metrics)."""

    min_cluster_size: int
    reason: str = ""


class RerunStageArgs(BaseModel):
    """Args for re-running a named stage (e.g. after reviewer feedback)."""

    stage: str
    reason: str = ""


class IngestArgs(BaseModel):
    """Args for a KB ingestion request. Precondition: validation must have passed."""

    kb_id: str


def make_ingest_tool(
    validation_passed: Callable[[], bool], do_ingest: Callable[[str], str]
) -> Callable[[IngestArgs], ToolResult]:
    """Build an ingest tool that REFUSES to run if validation did not pass (spec 6).

    This demonstrates the spec's hard rule: ``kb_ingest`` refuses to run on a file
    whose validation did not pass, regardless of what the planner asked for. The
    precondition is enforced here, in the tool.
    """

    def _ingest(args: IngestArgs) -> ToolResult:
        if not validation_passed():
            raise PreconditionError(
                "refusing to ingest: upstream validation did not pass"
            )
        job_id = do_ingest(args.kb_id)
        return ToolResult(status="ok", data={"job_id": job_id})

    return _ingest
