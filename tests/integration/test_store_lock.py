"""Phase 2.6.4 Batch B: one governing process per approval store.

The barricade is process memory. A revocation in process A cannot reach
process B's barrier, so the constraint "one governing process per store" has to
be enforced by something the operating system arbitrates — an exclusive lock on
``<store>.lock``. These tests exercise it across **real** process boundaries:
every second party is a separate interpreter started with ``sys.executable``
(see ``tests/support/lock_probe.py``), never a second object in this one.

Each test states what it is about, because the four cases look alike and are
not: exclusion, release, abnormal exit, and path aliasing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import PluginStoreLockedError
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle
from deskweave_plugin_wasm.locking import held_keys, lock_path_for
from deskweave_runtime import Runtime

PROBE = Path(__file__).resolve().parent.parent / "support" / "lock_probe.py"
#: The probe is a real process with real imports; this is a ceiling, not a wait.
PROBE_TIMEOUT = 120


class Holder:
    """A child process holding the store lock, driven by its ready file."""

    def __init__(self, store: str | Path, ready: Path, *, cwd: Path | None = None) -> None:
        self.store = store
        self.ready = ready
        command = [sys.executable, str(PROBE), "hold", "--store", str(store), "--ready", str(ready)]
        if cwd is not None:
            command += ["--cwd", str(cwd)]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_ready(self) -> dict[str, Any]:
        """Wait for the child to say it holds the lock. Not a timed sleep."""
        deadline = time.monotonic() + PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if self.ready.is_file():
                return json.loads(self.ready.read_text(encoding="utf-8"))
            if self.process.poll() is not None:
                _out, err = self.process.communicate()
                raise AssertionError(f"the holder exited without taking the lock: {err}")
            time.sleep(0.02)
        raise AssertionError("the holder never reported holding the lock")

    def _send(self, command: str) -> tuple[int, str]:
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        out, err = self.process.communicate(timeout=PROBE_TIMEOUT)
        return self.process.returncode, out + err

    def release(self) -> None:
        code, output = self._send("release")
        assert code == 0, output
        assert "released" in output

    def crash(self) -> None:
        code, _output = self._send("crash")
        assert code == 9, "the child was supposed to die without cleaning up"


@pytest.fixture
def holder(tmp_path: Path):
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    children: list[Holder] = []

    def start() -> Holder:
        child = Holder(store, tmp_path / "ready.json")
        children.append(child)
        child.wait_ready()
        return child

    yield store, start
    for child in children:
        if child.process.poll() is None:
            child.process.kill()
            child.process.communicate(timeout=PROBE_TIMEOUT)


def _lifecycle(runtime: Runtime, store: Path) -> PluginLifecycle:
    return PluginLifecycle(
        registry=default_registry(runtime.permissions, None),
        permissions=runtime.permissions,
        store=JsonApprovalStore(store),
    )


def _holds(store: Path) -> bool:
    """Does *this process* hold the lock for this store?

    Asked per store, never as "the registry is empty": other lifecycles in the
    same pytest process legitimately hold their own stores, and a global
    equality would report their locks as this test's failure.
    """
    return os.path.normcase(str(store.resolve())) in held_keys()


def _probe(store: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Run one probe process and return its verdict line.

    ``cwd`` is passed to the probe, not to ``subprocess``: the point is that the
    child resolves the store after changing directory itself.
    """
    command = [sys.executable, str(PROBE), "probe", "--store", store]
    if cwd is not None:
        command += ["--cwd", str(cwd)]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one verdict line, got {lines!r}"
    return json.loads(lines[0])


# -- exclusion --------------------------------------------------------------


def test_a_second_process_cannot_govern_a_held_store(holder, runtime: Runtime) -> None:
    store, start = holder
    child = start()

    with pytest.raises(PluginStoreLockedError) as caught:
        _lifecycle(runtime, store)

    # Structured, so a caller can report the two paths without parsing prose.
    assert caught.value.details["path"] == str(store.resolve())
    assert caught.value.details["lock_path"] == str(lock_path_for(store.resolve()))
    # The refusal left nothing behind for this store in this process.
    assert not _holds(store)
    assert child.process.poll() is None


def test_the_store_is_governable_once_the_holder_releases(holder, runtime: Runtime) -> None:
    store, start = holder
    child = start()
    with pytest.raises(PluginStoreLockedError):
        _lifecycle(runtime, store)

    child.release()

    lifecycle = _lifecycle(runtime, store)
    assert _holds(store)
    lifecycle.close()

    # The lock file is left in place on purpose: deleting it would let two
    # processes lock two different inodes under one name.
    assert lock_path_for(store.resolve()).is_file()
    assert not _holds(store)


def test_a_holder_that_dies_without_releasing_does_not_pin_the_store(
    holder, runtime: Runtime
) -> None:
    """No finally block runs in the child; only the OS can release the lock."""
    store, start = holder
    child = start()
    with pytest.raises(PluginStoreLockedError):
        _lifecycle(runtime, store)

    child.crash()

    deadline = time.monotonic() + PROBE_TIMEOUT
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            lifecycle = _lifecycle(runtime, store)
        except PluginStoreLockedError as exc:  # the OS has not finished bookkeeping yet
            last = exc
            time.sleep(0.05)
            continue
        lifecycle.close()
        return
    raise AssertionError(f"the store stayed locked after the holder died: {last}")


# -- path aliasing ----------------------------------------------------------


def test_a_relative_path_from_another_directory_names_the_same_store(
    holder, runtime: Runtime, tmp_path: Path
) -> None:
    """The lock is keyed on the *resolved* path, so a second cwd cannot alias it."""
    store, start = holder
    start()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    relative = os.path.relpath(store, elsewhere)
    assert not os.path.isabs(relative), "the probe must use a relative path"

    verdict = _probe(relative, cwd=elsewhere)

    assert verdict["ok"] is False, f"a second process governed the store: {verdict}"
    assert verdict["error"] == "locked"
    assert verdict["details"]["lock_path"] == str(lock_path_for(store.resolve()))


def test_a_relative_child_holder_blocks_the_absolute_parent(
    holder, runtime: Runtime, tmp_path: Path
) -> None:
    """The same aliasing in the other direction: the child holds via a relative path."""
    store, _start = holder
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    relative = os.path.relpath(store, elsewhere)
    child = Holder(relative, tmp_path / "ready-relative.json", cwd=elsewhere)
    try:
        ready = child.wait_ready()
        assert ready["lock_path"] == str(lock_path_for(store.resolve()))
        with pytest.raises(PluginStoreLockedError):
            _lifecycle(runtime, store)
    finally:
        if child.process.poll() is None:
            child.process.kill()
            child.process.communicate(timeout=PROBE_TIMEOUT)


# -- no silent degradation --------------------------------------------------


def test_a_refused_process_has_no_execution_entry_and_writes_nothing(
    holder, runtime: Runtime
) -> None:
    """Being refused is not "start unlocked": the store is never opened for writing.

    A second process must not get a lifecycle at all — no host, no executor, no
    dispatch guard — and the file it could not claim must be byte-identical
    afterwards. The holder keeps working, which is what "one governing process"
    means.
    """
    store, start = holder
    child = start()
    before = store.read_bytes()

    verdict = _probe(str(store))

    assert verdict["ok"] is False, verdict
    assert verdict["error"] == "locked"
    assert "held" not in verdict, "the refused process held a lock"
    assert store.read_bytes() == before, "the refused process wrote to the store"
    assert not _holds(store), "the refused attempt left a lock registered here"

    # The holder is not degraded by the attempt: once it lets go, the file is
    # exactly as it was and the store can be claimed by this process.
    child.release()
    assert store.read_bytes() == before
    parent = _lifecycle(runtime, store)
    try:
        assert _holds(store)
    finally:
        parent.close()
    assert store.read_bytes() == before


# -- path resolution --------------------------------------------------------
#
# The exclusion tests above pass because the operating system resolves a
# relative path itself. What resolution buys is identity: the same store reached
# two ways must produce one lock file and one registry key, or the in-process
# reference count — the thing that stops two surfaces deadlocking — counts two.


def test_a_relative_store_path_yields_the_resolved_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "approvals.json"
    store.write_text('{"plugins": {}}', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    relative = os.path.relpath(store, elsewhere)

    expected = store.resolve().with_name(store.resolve().name + ".lock")
    assert lock_path_for(relative) == expected
    assert lock_path_for(relative) == lock_path_for(store)


def test_a_relative_store_path_is_registered_under_its_resolved_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deskweave_plugin_wasm.locking import acquire, release

    store = tmp_path / "approvals.json"
    store.write_text('{"plugins": {}}', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    lock = acquire(os.path.relpath(store, elsewhere))
    try:
        # The same store by its absolute name is the same lock, not a second one.
        assert _holds(store)
    finally:
        release(lock)
    assert not _holds(store)


# -- the in-process side of the same rule -----------------------------------


def test_close_is_idempotent_and_only_the_last_holder_releases(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Two lifecycles over one store share one lock; it drops with the last one.

    The alternative — a lock per lifecycle — would deadlock the second surface
    against the first in the same process, which is not a security property but
    a crash.
    """
    store = tmp_path / "approvals.json"
    store.write_text('{"plugins": {}}', encoding="utf-8")

    first = _lifecycle(runtime, store)
    second = _lifecycle(runtime, store)
    try:
        assert _holds(store)

        first.close()
        assert _holds(store), "closing one lifecycle unlocked the store"

        first.close()  # idempotent, and must not drop the other holder's reference
        assert _holds(store)

        second.close()
        assert not _holds(store)
        second.close()
        assert not _holds(store)
    finally:
        first.close()
        second.close()

    # Left in place on purpose: unlinking it is how two processes end up locking
    # two different inodes under one name.
    assert lock_path_for(store).is_file()

    # The registry agreeing is not the same as the OS letting go. A separate
    # process is the only witness that can tell the difference.
    verdict = _probe(str(store))
    assert verdict["ok"] is True, f"close() did not release the lock: {verdict}"
    assert not _holds(store), "the probe's lock leaked into this process"
