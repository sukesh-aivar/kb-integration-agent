"""Tests for the per-project S3 conditional-write lock (B4.4)."""

from __future__ import annotations

import json

import pytest

from kb_agent.aws.fakes import FakeObjectStore
from kb_agent.lock import ProjectBusyError, ProjectLock

KEY = "projects/acme/_lock.json"


def _lock(store, run_id, **kw):
    return ProjectLock(store, KEY, run_id, wait_seconds=kw.pop("wait_seconds", 0),
                       poll_seconds=0.0, **kw)


def test_acquire_and_release() -> None:
    store = FakeObjectStore()
    lock = _lock(store, "run1")
    lock.acquire()
    assert store.exists(KEY)
    holder = json.loads(store.get_text(KEY))
    assert holder["run_id"] == "run1"
    lock.release()
    assert not store.exists(KEY)


def test_release_is_idempotent() -> None:
    store = FakeObjectStore()
    lock = _lock(store, "run1")
    lock.acquire()
    lock.release()
    lock.release()  # no raise


def test_contend_raises_project_busy() -> None:
    store = FakeObjectStore()
    _lock(store, "run1").acquire()  # held, not released
    with pytest.raises(ProjectBusyError):
        _lock(store, "run2", wait_seconds=0).acquire()


def test_stale_lock_is_taken_over() -> None:
    store = FakeObjectStore()
    # Write an already-expired lock from another run.
    store.put_text(KEY, json.dumps({
        "run_id": "old", "acquired_at": "2000-01-01T00:00:00+00:00",
        "expires_at": "2000-01-01T00:00:01+00:00",
    }))
    lock = _lock(store, "run2", wait_seconds=0)
    lock.acquire()  # steals the stale lock
    assert json.loads(store.get_text(KEY))["run_id"] == "run2"


def test_own_lock_reacquired_on_resume() -> None:
    store = FakeObjectStore()
    _lock(store, "run1").acquire()
    # Same run_id acquiring again (resume) must succeed, not block.
    lock2 = _lock(store, "run1", wait_seconds=0)
    lock2.acquire()
    assert lock2._held


def test_renew_updates_expiry() -> None:
    store = FakeObjectStore()
    lock = _lock(store, "run1")
    lock.acquire()
    before = json.loads(store.get_text(KEY))["expires_at"]
    lock.renew()
    after = json.loads(store.get_text(KEY))["expires_at"]
    # renew rewrites the payload (expiry is >= the first; equal only if same instant)
    assert after >= before


def test_release_does_not_stomp_a_taken_over_lock() -> None:
    store = FakeObjectStore()
    lock1 = _lock(store, "run1")
    lock1.acquire()
    # Another run takes over (simulate by overwriting with run2's payload).
    store.put_text(KEY, json.dumps({
        "run_id": "run2", "acquired_at": "2999-01-01T00:00:00+00:00",
        "expires_at": "2999-01-01T00:05:00+00:00",
    }))
    lock1.release()  # must NOT delete run2's lock
    assert json.loads(store.get_text(KEY))["run_id"] == "run2"
