"""Structured logging with ``run_id`` binding (spec 1 scaffold, 5.3, 7.4).

Uses structlog so every log line is structured JSON carrying the ``run_id``, the
stage, and whatever key/value context is bound. Two hard rules from the spec:

* Never log raw customer / ticket content at INFO - log IDs, counts and hashes
  (spec 7.4 security). Callers are responsible for not passing raw bodies; this
  module makes the structured style the path of least resistance.
* The ``run_id`` is bound once per run via :func:`bind_run_id` and then rides
  along on every subsequent log call from any module.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging to emit JSON lines.

    Idempotent: safe to call more than once (only the first call takes effect),
    which matters because both the CLI entrypoint and tests may call it.

    Args:
        level: Logging level name (e.g. ``"INFO"``, ``"DEBUG"``).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def bind_run_id(run_id: str) -> None:
    """Bind ``run_id`` to the context so every later log line carries it.

    Uses contextvars, so the binding is visible to all modules without threading
    the id through every function signature.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger, optionally named for a module/stage."""
    return structlog.get_logger(name)
