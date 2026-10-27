"""Tests for the Part B ObjectStore extensions on FakeObjectStore (B3/B4)."""

from __future__ import annotations

import hashlib

from kb_agent.aws.fakes import FakeObjectStore


def test_head_returns_content_hash() -> None:
    store = FakeObjectStore()
    store.put_text("a.txt", "hello")
    meta = store.head("a.txt")
    assert meta is not None
    assert meta["content_hash"] == hashlib.sha256(b"hello").hexdigest()
    assert store.head("missing") is None


def test_put_if_absent_is_conditional() -> None:
    store = FakeObjectStore()
    assert store.put_if_absent("k", b"first") is True
    assert store.put_if_absent("k", b"second") is False
    assert store.get_bytes("k") == b"first"  # not overwritten


def test_copy_preserves_bytes_and_hash() -> None:
    store = FakeObjectStore(bucket="b")
    store.put_text("src", "payload")
    uri = store.copy("s3://b/src", "dst")
    assert uri == "s3://b/dst"
    assert store.get_text("dst") == "payload"
    assert store.head("dst")["content_hash"] == store.head("src")["content_hash"]


def test_delete_removes_object_and_meta() -> None:
    store = FakeObjectStore()
    store.put_text("k", "v")
    store.delete("k")
    assert not store.exists("k")
    assert store.head("k") is None


def test_list_uri_returns_full_uris() -> None:
    store = FakeObjectStore(bucket="b")
    store.put_text("p/a.txt", "x")
    store.put_text("p/b.txt", "y")
    uris = store.list_uri("s3://b/p/")
    assert uris == ["s3://b/p/a.txt", "s3://b/p/b.txt"]
