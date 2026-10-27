"""Per-project concurrency lock over S3 conditional writes (Part B B4.4).

Two runs for the same ``project_id`` must not provision or ingest at the same
time (the service allows one ingestion job per data source at a time, and
create-or-verify races could double-create resources). This is a cooperative
lock, not a distributed consensus system - good enough because the contention is
rare (the same customer's project touched twice) and every holder self-expires.

Mechanism: ``PutObject`` with ``If-None-Match: *`` writes the lock object only if
it does not already exist (:meth:`ObjectStore.put_if_absent`). The lock carries
``{run_id, acquired_at, expires_at}``. A lock whose ``expires_at`` has passed is
stale and may be taken over. The holder renews from the heartbeat thread and
releases in a ``finally``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from kb_agent.aws.interfaces import ObjectStore
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


class ProjectBusyError(Exception):
    """Another live run holds the project lock past the wait budget (project_busy)."""


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectLock:
    """A cooperative, self-expiring lock on one project's provisioning/ingestion."""

    def __init__(
        self,
        store: ObjectStore,
        lock_key: str,
        run_id: str,
        *,
        ttl_seconds: int = 300,
        wait_seconds: int = 900,
        poll_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._key = lock_key
        self._run_id = run_id
        self._ttl = ttl_seconds
        self._wait = wait_seconds
        self._poll = poll_seconds
        self._held = False

    # --- context manager -----------------------------------------------------

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # --- operations ----------------------------------------------------------

    def _payload(self) -> bytes:
        now = _now()
        return json.dumps({
            "run_id": self._run_id,
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self._ttl)).isoformat(),
        }).encode("utf-8")

    def acquire(self, *, emit: Callable[[str], None] | None = None) -> None:
        """Acquire the lock, waiting (with backoff) up to ``wait_seconds``.

        Raises :class:`ProjectBusyError` if a live (non-expired) lock is still
        held by another run after the wait budget.
        """
        deadline = time.monotonic() + self._wait
        announced = False
        while True:
            if self._store.put_if_absent(self._key, self._payload()):
                self._held = True
                logger.info("project_lock_acquired", key=self._key, run_id=self._run_id)
                return
            # Someone holds it. If it is OUR lock (resumed run) or it is stale,
            # take it over; otherwise wait.
            holder = self._read()
            if holder is None:
                continue  # race: it vanished between put and read; retry immediately
            if holder.get("run_id") == self._run_id or self._is_stale(holder):
                self._steal()
                self._held = True
                logger.info("project_lock_taken_over", key=self._key, run_id=self._run_id,
                            previous=holder.get("run_id"))
                return
            if not announced and emit:
                emit("Another run is updating this project; waiting")
                announced = True
            if time.monotonic() > deadline:
                raise ProjectBusyError(
                    f"project is busy: run {holder.get('run_id')!r} holds the lock"
                )
            time.sleep(self._poll)

    def renew(self) -> None:
        """Refresh the lock's expiry (called from the heartbeat thread)."""
        if not self._held:
            return
        # Overwrite in place (we are the holder); a plain put is fine.
        self._store.put_bytes(self._key, self._payload())

    def release(self) -> None:
        """Release the lock if we hold it (idempotent; safe in a finally)."""
        if not self._held:
            return
        self._held = False
        holder = self._read()
        # Only delete if it is still ours (never stomp a lock that was taken over
        # after our TTL expired).
        if holder is not None and holder.get("run_id") != self._run_id:
            logger.warning("project_lock_not_ours_on_release", key=self._key)
            return
        try:
            self._store.delete(self._key)
        except Exception:  # noqa: BLE001 - release must never raise
            logger.warning("project_lock_delete_failed", key=self._key)
        logger.info("project_lock_released", key=self._key, run_id=self._run_id)

    # --- helpers -------------------------------------------------------------

    def _read(self) -> dict | None:
        try:
            if not self._store.exists(self._key):
                return None
            return json.loads(self._store.get_text(self._key))
        except Exception:  # noqa: BLE001 - a corrupt/half-written lock reads as absent
            return None

    def _is_stale(self, holder: dict) -> bool:
        if holder.get("released"):
            return True
        try:
            expires = datetime.fromisoformat(holder["expires_at"])
        except (KeyError, ValueError):
            return True  # malformed -> treat as stale
        return _now() > expires

    def _steal(self) -> None:
        """Overwrite the lock with our own payload (caller decided it is takeable)."""
        self._store.put_bytes(self._key, self._payload())
