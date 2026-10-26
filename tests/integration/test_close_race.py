"""Phase 2.6.7a: the store lock must outlive the operations it protects.

Both tests are **expected to fail** against the current implementation. They are
written first, deliberately: 2.6.6 made ``close()`` refuse every *new* write, but
the lock is released synchronously, so an operation already parked inside an
``await`` keeps running — and keeps touching stores — after the lock is gone. A
test that only asserts "close() then write raises" cannot see that; these two
drive the exact interleaving.

How the race is made deterministic (no sleeps, no retries):

* ``GatedAuthority.revoke`` signals ``reached`` from inside the call and then
  waits on a gate, so the caller knows the operation is parked at exactly the
  point where the approval record is written and the grants write has not
  happened yet;
* the competitor is a **separate process** (``tests/support/store_lock.py``), not
  a second object in this interpreter: an OS file lock is reference counted per
  process, so an in-process second handle would prove nothing;
* ``asyncio.wait_for`` is used only as a ceiling on a wait that must complete, so
  a failure reads "this never happened", never "this was slow".

What each test separates:

* test 1 asserts the **lock** is still held while a revocation is in flight, and
  that the revocation nevertheless ran to completion (``revoked_grants``), so a
  pass cannot come from aborting the operation early;
* test 2 asserts the **pin file** survives when it was replaced after the
  uninstall recorded its decision. The replacement is written out of band on
  purpose: the lock is advisory, so a non-cooperating writer is exactly the case
  a late ``unlink()`` has to survive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from deskweave_plugin_wasm.errors import PluginLifecycleError
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import INSTALL_RECORD_NAME, sha256_file
from deskweave_plugin_wasm.lifecycle import InstallState, JsonApprovalStore, PluginLifecycle
from deskweave_plugin_wasm.locking import held_keys
from support import wasm_plugins as fixtures
from support.inflight import GatedAuthority
from support.store_lock import probe

VALID = "reference.metadata.validate"
PLUGIN = "close-race"

#: A ceiling on waits that must complete. Reaching it means the interleaving never
#: happened, which is a failure of the test's premise, not a slow machine.
WAIT_S = 10.0


def _manifest(directory: Path) -> Path:
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        directory, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
    )
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=PLUGIN,
        wasm_path=module.name,
        wasm_sha256=digest,
        version="1.0.0",
        requested_capabilities=[VALID],
        input_schema={},
        output_schema={},
    )
    manifest = module.with_suffix(".json")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


async def _approved(directory: Path) -> tuple[PluginLifecycle, GatedAuthority, Path, Path]:
    """An installed, approved plugin over a real JSON store, plus its pin path."""
    authority = GatedAuthority()
    store_path = directory / "approvals.json"
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None),
        permissions=authority,
        store=JsonApprovalStore(store_path),
    )
    manifest = _manifest(directory)
    await lifecycle.install(manifest)
    await lifecycle.approve(PLUGIN, [VALID])
    return lifecycle, authority, store_path, manifest.parent / INSTALL_RECORD_NAME


def _key(store_path: Path) -> str:
    return os.path.normcase(str(store_path.resolve()))


async def test_a_close_during_a_revocation_does_not_release_lock_early(
    tmp_path: Path,
) -> None:
    """While a revocation is in flight, no other process may govern the store.

    The failure this pins down: ``close()`` releases the OS lock immediately, and
    the revocation is still parked inside ``permissions.revoke`` — so a second
    process takes the store while this one is about to write the grants table.
    """
    lifecycle, authority, store_path, _pin = await _approved(tmp_path)
    try:
        key = _key(store_path)
        assert key in held_keys(), "precondition: the store is governed after approve()"

        task = asyncio.create_task(lifecycle.revoke(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        # Parked with the record written and the grants write still outstanding.
        completed_when_parked = authority.completed

        lifecycle.close()
        authority.mark_closed()

        # A real second process tries to govern the same store while the first
        # operation is still running. It must be refused.
        verdict = probe(store_path)

        authority.release()
        report = await asyncio.wait_for(task, timeout=WAIT_S)

        assert completed_when_parked == 0, (
            "the revocation was not actually parked before the grants write; "
            "the interleaving this test needs did not happen"
        )
        assert verdict.get("error") == "locked", (
            "close() released the store lock while a revocation was still in flight: "
            f"a second process took the store ({verdict}). That is exactly the window "
            "close() claims to prevent — another process writing the store while this "
            "one still writes the grants."
        )
        assert report.revoked_grants, (
            "the revocation did not run to completion, so the lock assertion above "
            "would be satisfied by an aborted operation rather than by a correct one"
        )
    finally:
        lifecycle.close()


async def test_an_uninstall_in_flight_keeps_the_store_lock_until_it_returns(
    tmp_path: Path,
) -> None:
    """The same invariant for uninstall: the lock outlives the operation.

    Uninstall writes three places (record, grants, pin). The record is written
    before the parked await, so the two later writes are exactly the ones that
    would land after an early release.
    """
    lifecycle, authority, store_path, pin = await _approved(tmp_path)
    try:
        key = _key(store_path)
        task = asyncio.create_task(lifecycle.uninstall(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)

        lifecycle.close()
        verdict = probe(store_path)
        held_in_process = key in held_keys()

        authority.release()
        record = await asyncio.wait_for(task, timeout=WAIT_S)

        assert held_in_process, "close() released the store while an uninstall was in flight"
        assert verdict.get("error") == "locked", (
            f"a second process took the store while an uninstall was still writing: {verdict}"
        )
        assert record.state is InstallState.UNINSTALLED
        assert not pin.exists(), "the pin is this operation's own; it must be removed"
        assert key not in held_keys(), (
            "the lock was not released after the last in-flight operation returned"
        )
    finally:
        lifecycle.close()


async def test_aclose_waits_for_in_flight_work_and_closes_the_host(
    tmp_path: Path,
) -> None:
    """``aclose()`` is not a spelling of ``close()``: it waits, then closes the host."""
    lifecycle, authority, store_path, _pin = await _approved(tmp_path)
    key = _key(store_path)
    task = asyncio.create_task(lifecycle.revoke(PLUGIN))
    await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)

    closer = asyncio.create_task(lifecycle.aclose())
    # Many scheduling turns, so an aclose() that does *not* wait has every chance
    # to return. This is a spin of yields, not a delay: if it can finish, fifty
    # turns is far more than it needs; if it is waiting on the gate, no number of
    # turns helps. One turn would not be enough to tell the two apart.
    for _ in range(50):
        await asyncio.sleep(0)
    assert not closer.done(), "aclose() returned while an operation was still in flight"
    assert key in held_keys(), "aclose() released the store before the operation finished"
    assert lifecycle.closed, "aclose() must stop accepting work immediately"

    authority.release()
    leftovers = await asyncio.wait_for(closer, timeout=WAIT_S)
    report = await asyncio.wait_for(task, timeout=WAIT_S)

    assert report.revoked_grants, "the revocation must have run to completion"
    assert leftovers == 0, f"the host reported {leftovers} operation(s) still running"
    assert key not in held_keys(), "the store must be released once aclose() returns"
    assert lifecycle.host.executor._closed is True, (
        "aclose() must close the host executor it owns, not only the store lock"
    )
    assert await lifecycle.aclose() == 0, "aclose() must be idempotent"


async def test_a_cancelled_operation_still_releases_the_store_lock(tmp_path: Path) -> None:
    """Cancellation runs the finally block, so the count cannot leak."""
    lifecycle, authority, store_path, _pin = await _approved(tmp_path)
    try:
        key = _key(store_path)
        task = asyncio.create_task(lifecycle.revoke(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        lifecycle.close()
        assert key in held_keys(), "precondition: the lock is held by the in-flight operation"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert lifecycle.inflight == 0, "the in-flight count leaked across a cancellation"
        assert key not in held_keys(), (
            "the lock stayed held after the last operation was cancelled; a cancelled "
            "operation is still an operation that ended"
        )
        assert await lifecycle.aclose() == 0
    finally:
        lifecycle.close()


async def test_an_operation_that_raises_still_releases_the_store_lock(
    tmp_path: Path,
) -> None:
    """A failing grants store must not strand the lock — or the record.

    Since Phase 2.6.8b (C2) the failure *surfaces* as ``plugin_lifecycle`` with
    ``revocation_persisted=True`` / ``grants_withdrawn=False`` instead of escaping
    as the authority's own exception; the original is chained. That is asserted
    here as well, so this test still fails if the classification is dropped —
    the subject of the test (lock, counter, record) is unchanged.
    """
    lifecycle, authority, store_path, _pin = await _approved(tmp_path)
    try:
        key = _key(store_path)
        authority.fail_with = RuntimeError("test: the grants store went away")
        task = asyncio.create_task(lifecycle.revoke(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        lifecycle.close()
        assert key in held_keys()

        authority.release()
        with pytest.raises(PluginLifecycleError) as caught:
            await asyncio.wait_for(task, timeout=WAIT_S)

        assert caught.value.details["revocation_persisted"] is True
        assert caught.value.details["grants_withdrawn"] is False
        assert isinstance(caught.value.__cause__, RuntimeError), (
            "the authority's own failure must be chained, not replaced: "
            f"found {type(caught.value.__cause__).__name__}"
        )
        assert lifecycle.inflight == 0
        assert key not in held_keys(), "the lock stayed held after the operation failed"
        state = json.loads(store_path.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"]
        assert state == "revoked", (
            "the decision is written before the grants, so a grants failure must leave "
            f"the record refusing the plugin; found {state!r}"
        )
    finally:
        lifecycle.close()


async def test_approve_cannot_record_a_decision_after_close(tmp_path: Path) -> None:
    """approve() is not exempt: it must not write the store after close().

    A narrowing approval revokes the grants it drops *before* writing the
    decision, which is the parked point. Closing there means the grants are gone
    and the record still holds the previous decision — fewer grants than the
    record says, never more.
    """
    lifecycle, authority, store_path, _pin = await _approved(tmp_path)
    try:
        key = _key(store_path)
        task = asyncio.create_task(lifecycle.approve(PLUGIN, []))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        lifecycle.close()
        verdict = probe(store_path)

        authority.release()
        with pytest.raises(PluginLifecycleError) as caught:
            await asyncio.wait_for(task, timeout=WAIT_S)

        assert caught.value.details["approval_saved"] is False
        assert caught.value.details["revoked_stale"] == [VALID]
        cause = caught.value.__cause__
        assert isinstance(cause, PluginLifecycleError), (
            f"the refusal must be chained, found {type(cause).__name__}"
        )
        assert cause.details["lifecycle"] == "closed"
        assert verdict.get("error") == "locked", (
            f"a second process took the store while approve() was in flight: {verdict}"
        )
        assert key not in held_keys()
        state = json.loads(store_path.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"]
        assert state == "approved", "the refused decision must not have been written"
        assert not any(authority.granted.values()), (
            "the narrowing approval already revoked its grants; that direction is safe, "
            "but the test asserts it happened rather than assuming it"
        )
    finally:
        lifecycle.close()


async def test_an_uninstall_in_flight_does_not_delete_a_replacement_pin(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pin written after the record decision must survive the late unlink().

    The uninstall has already recorded ``uninstalled`` and is parked before the
    grants withdrawal. At that moment someone replaces the pin with **a pin for a
    different build** (different plugin id and digest); the unlink that runs later
    must not delete it — that file belongs to an install this operation never
    verified.

    A pin whose digest is unchanged is still *this* install's pin, whatever its
    version field says, so doctoring only that field would not be a replacement.
    """

    lifecycle, authority, store_path, pin = await _approved(tmp_path)
    try:
        assert pin.is_file(), "precondition: install() wrote a pin"

        task = asyncio.create_task(lifecycle.uninstall(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        completed_when_parked = authority.completed

        lifecycle.close()
        authority.mark_closed()

        # What a competing writer does: try the lock (advisory, so this is
        # evidence rather than the assertion), then write its own pin.
        verdict = probe(store_path)
        replacement = json.loads(pin.read_text(encoding="utf-8"))
        replacement["plugin_id"] = f"{PLUGIN}-replacement"
        replacement["version"] = "2.0.0"
        replacement["wasm_sha256"] = "f" * 64
        replacement_bytes = json.dumps(replacement, indent=2, sort_keys=True).encode()
        pin.write_bytes(replacement_bytes)

        authority.release()
        with caplog.at_level(logging.WARNING, logger="deskweave.wasm.lifecycle"):
            record = await asyncio.wait_for(task, timeout=WAIT_S)

        assert completed_when_parked == 0, (
            "the uninstall was not actually parked before the grants withdrawal; "
            "the interleaving this test needs did not happen"
        )
        assert record.state is InstallState.UNINSTALLED, (
            "the uninstall did not run to completion, so a surviving pin would only "
            "mean the operation never reached the unlink"
        )
        assert pin.is_file(), (
            "the late unlink deleted a pin written by someone else "
            f"(the competing lock probe said {verdict}). The identity check either "
            "did not run or treated a different install as this one."
        )
        assert pin.read_bytes() == replacement_bytes, (
            "the pin on disk is not the one the competing writer wrote; something "
            "rewrote or removed content this operation never verified"
        )
        assert "kept the install record" in caplog.text, (
            "the file was kept but nothing said so; a silent refusal to delete is "
            "indistinguishable from a successful cleanup"
        )
    finally:
        lifecycle.close()
