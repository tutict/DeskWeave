"""Phase 2.6.9b: ``aclose()`` reliability — R1, R2, R3.

Written before the fix, so each red is attributable:

* **R1** — awaiting ``aclose()`` on a different event loop than the in-flight
  operation must return once that operation finishes. Phase 2.6.9a measured that
  it does not: the waiter parks forever, the store lock is already released, and
  the host is never closed.
* **R2** — a ``host.aclose()`` failure must be reported the way every other
  lifecycle failure is (``plugin_lifecycle`` with detail fields and the original
  chained), must stay retryable, and must not be confused with "the store lock was
  released".
* **R3** — cancellation must not fabricate a success and must leave a state that a
  later ``aclose()`` can finish.

Isolation: R1 runs in a child process (``tests/support/cross_loop_probe.py``)
with a hard timeout, because a hung waiter in-process would leak a thread, a loop
and a held store lock into the rest of the session. Every other test here is
bounded with ``asyncio.wait_for`` and releases its gate in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deskweave_plugin_wasm.errors import PluginLifecycleError
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle
from deskweave_plugin_wasm.locking import held_keys
from support import wasm_plugins as fixtures
from support.inflight import GatedAuthority

VALID = "reference.metadata.validate"
PLUGIN = "aclose-reliability"
PROBE = Path(__file__).resolve().parents[1] / "support" / "cross_loop_probe.py"

#: A ceiling on waits that must complete. Reaching it means the thing never
#: happened, which is a failure of the premise, not a slow machine.
WAIT_S = 10.0
#: Hard ceiling for the child process. The child has its own, shorter deadlines.
HARD_S = 90.0


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


async def _approved(
    directory: Path, *, gate_open: bool = False
) -> tuple[PluginLifecycle, GatedAuthority, Path, Path]:
    """An installed, approved plugin over a real JSON store."""
    authority = GatedAuthority()
    if gate_open:
        authority.release()
    store_path = directory / "approvals.json"
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None),
        permissions=authority,
        store=JsonApprovalStore(store_path),
    )
    manifest = _manifest(directory)
    await lifecycle.install(manifest)
    await lifecycle.approve(PLUGIN, [VALID])
    return lifecycle, authority, store_path, manifest


def _key(store_path: Path) -> str:
    return os.path.normcase(str(store_path.resolve()))


def _counting_host_close(lifecycle: PluginLifecycle) -> list[int]:
    """Count host-shutdown attempts while still calling the real one."""
    calls: list[int] = []
    real = lifecycle.host.aclose

    async def wrapper() -> int:
        calls.append(1)
        return await real()

    lifecycle.host.aclose = wrapper  # type: ignore[method-assign]
    return calls


def _run_probe(directory: Path) -> tuple[int, dict[str, object], str]:
    """Run the cross-loop child under a hard ceiling; always reap it."""
    process = subprocess.Popen(
        [sys.executable, str(PROBE), str(directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = process.communicate(timeout=HARD_S)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError(
            f"the cross-loop probe was killed after {HARD_S}s; stdout={out[-800:]!r} "
            f"stderr={err[-800:]!r}"
        ) from None
    finally:
        if process.poll() is None:  # pragma: no cover - communicate() reaps normally
            process.kill()
            process.communicate()

    verdict: dict[str, object] = {}
    for line in out.splitlines():
        if line.startswith("VERDICT "):
            verdict = json.loads(line[len("VERDICT ") :])
    assert verdict, f"the probe printed no verdict; stdout={out[-800:]!r} stderr={err[-800:]!r}"
    return process.returncode, verdict, err


# -- R1: the wake-up must cross loops -----------------------------------------


def test_aclose_awaited_on_a_second_loop_returns(tmp_path: Path) -> None:
    """The waiter lives on loop B; the operation that clears the count is on loop A.

    ``_drained`` is woken by the operation leaving, and that happens on another
    thread. A wake-up mechanism that is not told about the waiter's loop parks the
    waiter forever — measured in 2.6.9a at 8 s, with the store already released
    and the host still open.
    """
    code, verdict, err = _run_probe(tmp_path)

    assert verdict.get("op_completed") is True, (
        f"the operation never finished: {verdict} {err[-400:]}"
    )
    assert code == 0, f"the child reported a deadline instead of a return: {verdict}"
    assert verdict.get("aclose_returned") is True, (
        "aclose() awaited on a second event loop must return once the in-flight "
        f"operation finishes; it was still pending at the deadline: {verdict}"
    )
    assert verdict.get("aclose_value") == 0, (
        f"a drained shutdown reports 0 leftovers; got {verdict.get('aclose_value')!r}"
    )
    assert verdict.get("host_close_calls") == 1, (
        "the host must be closed exactly once by the call that returns: "
        f"{verdict.get('host_close_calls')!r}"
    )
    assert verdict.get("store_lock_released") is True, (
        f"the store lock must be released before aclose() returns: {verdict}"
    )


async def test_aclose_on_the_same_loop_still_returns(tmp_path: Path) -> None:
    """Control: the single-loop path keeps working, so R1 is not fixed by luck.

    Without this, "the cross-loop test passes" could mean "the drain no longer
    waits at all", which would be a worse bug than the one being fixed.
    """
    lifecycle, authority, store_path, _manifest_path = await _approved(tmp_path)
    key = _key(store_path)
    calls = _counting_host_close(lifecycle)
    try:
        task = asyncio.create_task(lifecycle.revoke(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)

        closer = asyncio.create_task(lifecycle.aclose())
        for _ in range(50):
            await asyncio.sleep(0)
        assert not closer.done(), "aclose() returned while an operation was still in flight"
        assert key in held_keys(), "the store was released before the operation finished"

        authority.release()
        await asyncio.wait_for(task, timeout=WAIT_S)
        leftovers = await asyncio.wait_for(closer, timeout=WAIT_S)

        assert leftovers == 0
        assert calls == [1], f"host shutdown attempts: {calls}"
        assert key not in held_keys()
    finally:
        authority.release()
        lifecycle.close()


# -- R2: a failed host shutdown is classified, and stays retryable -------------


async def test_a_host_shutdown_failure_is_structured_and_retryable(tmp_path: Path) -> None:
    """The store lock is already gone when the host refuses to close.

    That asymmetry is the whole point: ``aclose()`` is ``close()`` (which releases
    the store) *then* host shutdown, so "the lock is released" must never be read
    as "the host is closed".
    """
    lifecycle, _authority, store_path, _manifest_path = await _approved(tmp_path, gate_open=True)
    key = _key(store_path)
    real = lifecycle.host.aclose
    calls: list[int] = []

    async def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("injected: the host would not shut down")
        return await real()

    lifecycle.host.aclose = flaky  # type: ignore[method-assign]
    try:
        with pytest.raises(PluginLifecycleError) as caught:
            await asyncio.wait_for(lifecycle.aclose(), timeout=WAIT_S)

        failure = caught.value
        assert failure.code == "plugin_lifecycle", (
            f"a shutdown failure must use the lifecycle error code; got {failure.code!r}"
        )
        assert failure.details.get("host_closed") is False, (
            "the caller has to be able to tell that the host is *not* closed; "
            f"details were {failure.details!r}"
        )
        assert isinstance(failure.__cause__, RuntimeError), (
            "the host's own failure must be chained, not replaced: "
            f"found {type(failure.__cause__).__name__}"
        )

        # The two facts are separate, and both are observable.
        assert key not in held_keys(), "the store lock is released before host shutdown runs"
        assert lifecycle.closed is True
        assert lifecycle.inflight == 0
        assert calls == [1], f"the first attempt must be the only one so far: {calls}"

        second = await asyncio.wait_for(lifecycle.aclose(), timeout=WAIT_S)
        assert second == 0, f"the retry must complete the shutdown; got {second!r}"
        assert calls == [1, 1], f"the retry must actually call host shutdown again: {calls}"
    finally:
        lifecycle.host.aclose = real  # type: ignore[method-assign]
        lifecycle.close()


# -- R3: cancellation leaves something a later call can finish -----------------


async def test_a_cancelled_aclose_during_the_drain_can_be_finished_later(tmp_path: Path) -> None:
    """Cancelling the waiter must not cancel the operation, and must not claim success."""
    lifecycle, authority, store_path, _manifest_path = await _approved(tmp_path)
    key = _key(store_path)
    calls = _counting_host_close(lifecycle)
    try:
        task = asyncio.create_task(lifecycle.revoke(PLUGIN))
        await asyncio.wait_for(authority.reached.wait(), timeout=WAIT_S)
        closer = asyncio.create_task(lifecycle.aclose())
        for _ in range(20):
            await asyncio.sleep(0)

        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, timeout=WAIT_S)

        assert calls == [], "a cancelled drain must not have attempted host shutdown"
        assert lifecycle.closed is True, "close() had already taken effect"
        assert key in held_keys(), "the parked operation still needs its store lock"

        authority.release()
        await asyncio.wait_for(task, timeout=WAIT_S)
        assert lifecycle.inflight == 0
        assert key not in held_keys()

        recovered = await asyncio.wait_for(lifecycle.aclose(), timeout=WAIT_S)
        assert recovered == 0
        assert calls == [1], f"the later call must close the host once: {calls}"
    finally:
        authority.release()
        lifecycle.close()


async def test_a_cancelled_aclose_during_host_shutdown_can_be_finished_later(
    tmp_path: Path,
) -> None:
    """Cancellation inside host shutdown must propagate, and the retry must finish.

    The gate makes the cancellation point deterministic. What must not happen is a
    swallowed cancellation, a fabricated success, or a lifecycle that can no
    longer be closed.
    """
    lifecycle, _authority, store_path, _manifest_path = await _approved(tmp_path, gate_open=True)
    key = _key(store_path)
    real = lifecycle.host.aclose
    entered = asyncio.Event()
    gate = asyncio.Event()
    attempts: list[int] = []

    async def parked() -> int:
        attempts.append(1)
        entered.set()
        await gate.wait()
        return await real()

    lifecycle.host.aclose = parked  # type: ignore[method-assign]
    try:
        closer = asyncio.create_task(lifecycle.aclose())
        await asyncio.wait_for(entered.wait(), timeout=WAIT_S)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, timeout=WAIT_S)

        assert attempts == [1], "the host shutdown was attempted exactly once"
        assert lifecycle.closed is True
        assert key not in held_keys(), "the store was released before the cancel landed"

        gate.set()
        lifecycle.host.aclose = real  # type: ignore[method-assign]
        recovered = await asyncio.wait_for(lifecycle.aclose(), timeout=WAIT_S)
        assert recovered == 0, f"the later call must finish the shutdown; got {recovered!r}"
    finally:
        gate.set()
        lifecycle.host.aclose = real  # type: ignore[method-assign]
        lifecycle.close()
