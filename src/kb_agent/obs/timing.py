"""Handling-time measurement (spec 5.1 "savings gauge").

Records wall-clock duration per stage and for the whole run. Made a decorator so
no stage can forget (spec 5.1). The SOW compares these against a baseline Ingram
Micro supplies.

A :class:`StageTimer` collects durations into a dict that the run manifest and
report read. The ``@timed`` decorator attaches to a timer passed via the first
argument's convention, but to keep stages as plain functions we expose an
explicit context manager too, which the pipeline uses.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager


class StageTimer:
    """Accumulates per-stage wall-clock durations for a run."""

    def __init__(self) -> None:
        self._durations: dict[str, float] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        """Time a block and record it under ``stage``.

        Usage:
            with timer.measure("cluster"):
                ...
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            self._durations[stage] = round(time.perf_counter() - start, 4)

    @property
    def durations(self) -> dict[str, float]:
        """A copy of the recorded per-stage durations in seconds."""
        return dict(self._durations)

    @property
    def total(self) -> float:
        """Sum of all recorded stage durations."""
        return round(sum(self._durations.values()), 4)
