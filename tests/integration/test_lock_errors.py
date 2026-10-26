"""Phase 2.6.6 P2: a busy store and a broken lock file are different answers.

``plugin_store_locked`` used to be raised for every failure to create the lock
file, so a read-only directory looked like "another process is governing this
store" — a retry loop over a condition that would never clear. Only real
contention may produce it; everything else is ``PluginLifecycleError``, and every
failure path must leave no descriptor and no in-process reference behind.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from deskweave_plugin_wasm.errors import PluginLifecycleError, PluginStoreLockedError
from deskweave_plugin_wasm.locking import acquire, held_keys, lock_path_for, release
from support.store_lock import Holder


def _lock_path(store: Path) -> Path:
    return lock_path_for(store)


def _key(store: Path) -> str:
    return os.path.normcase(str(store.resolve()))


def test_contention_is_still_a_lock_conflict(tmp_path: Path) -> None:
    """The one failure that *is* "busy" keeps its own error and its own code."""
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")

    with Holder(store, tmp_path / "ready.json") as holder:
        holder.wait_ready()
        with pytest.raises(PluginStoreLockedError) as caught:
            acquire(store)
        assert caught.value.code == "plugin_store_locked"
        assert "another process" in caught.value.message
        # Refused, and nothing of ours is left holding anything. Asked per
        # store: other lifecycles in this process legitimately hold their own.
        assert _key(store) not in held_keys()


def test_a_lock_path_that_cannot_be_opened_is_not_a_conflict(tmp_path: Path) -> None:
    """A directory where the lock file should be: broken, not busy.

    The failure comes from ``os.open`` — the same call a read-only directory, an
    exhausted handle table or a full disk would fail in — so this is the real
    classification path, not a mock of it.
    """
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    _lock_path(store).mkdir()

    with pytest.raises(PluginLifecycleError) as caught:
        acquire(store)
    assert not isinstance(caught.value, PluginStoreLockedError)
    assert caught.value.code == "plugin_lifecycle"
    assert "refusing to run unlocked" in caught.value.message
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.details["lock_path"].endswith(".lock")
    assert _key(store) not in held_keys()


def test_a_directory_that_cannot_be_created_is_not_a_conflict(tmp_path: Path) -> None:
    """The parent-creation step fails on a path whose ancestor is a file."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    store = blocker / "nested" / "approvals.json"

    with pytest.raises(PluginLifecycleError) as caught:
        acquire(store)
    assert not isinstance(caught.value, PluginStoreLockedError)
    assert isinstance(caught.value.__cause__, OSError)
    assert _key(store) not in held_keys(), "the store was never reachable, so nothing may be held"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="only Windows refuses to unlink a file another handle opened without FILE_SHARE_DELETE",
)
def test_a_failed_acquire_leaves_no_descriptor(tmp_path: Path) -> None:
    """A leaked descriptor would keep the lock file undeletable after the holder left.

    On POSIX ``unlink`` succeeds regardless, so the assertion would be vacuous
    there; this is why it is Windows-only rather than claimed cross-platform.
    """
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    holder = Holder(store, tmp_path / "ready.json")
    holder.wait_ready()
    with pytest.raises(PluginStoreLockedError):
        acquire(store)
    holder.stop()

    os.unlink(_lock_path(store))  # raises PermissionError if a descriptor survived


def test_acquire_creates_the_store_directory_and_says_so(tmp_path: Path) -> None:
    """The documented side effect: one idempotent ``mkdir``, nothing else."""
    store = tmp_path / "fresh" / "nested" / "approvals.json"
    assert not store.parent.exists()

    lock = acquire(store)
    try:
        assert store.parent.is_dir()
        assert lock.lock_path.is_file()
        assert lock.lock_path.parent == store.parent
        assert sorted(p.name for p in store.parent.iterdir()) == ["approvals.json.lock"]
    finally:
        release(lock)

    second = acquire(store)
    try:
        assert store.parent.is_dir(), "the second acquire is idempotent"
    finally:
        release(second)


def test_release_does_not_delete_the_lock_file(tmp_path: Path) -> None:
    """Deleting it would let two processes lock two different inodes under one name."""
    store = tmp_path / "approvals.json"
    lock = acquire(store)
    path = lock.lock_path
    release(lock)
    assert lock.released is True
    assert path.is_file()
