"""Execution governance: the event loop, cancellation, cost and concurrency.

Phase 2.3 proved a plugin cannot escape the boundary. Phase 2.4 asks a different
question: once a call is *authorised*, what does the host actually do — and does
its report match reality?

Every test here drives real capabilities through the real WASM guest. Nothing is
mocked, because the properties at stake are about threads, the event loop and
wall-clock time, and a mock would assert the shape of the code rather than its
behaviour.

The recurring distinction, and the reason this file is long:

    timed_out  != cancelled
    admitted   != executed
    we asked   != it stopped
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import deskweave_plugin_wasm.runtime as runtime_module
from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    HostCapabilityRegistry,
    HostExecutor,
    PluginHostError,
    PluginResourceError,
    WasmPluginHost,
    principal_for,
)
from deskweave_runtime import Runtime
from support import host_capabilities as caps
from support import wasm_plugins as fixtures

PLUGIN_ID = "governed"


def _request(capability: str, payload: dict | None = None) -> str:
    return json.dumps({"capability": capability, "payload": payload or {}})


def _plugin(tmp_path: Path, wat: str, capability: str, **limits: int) -> Path:
    return fixtures.write_plugin(
        tmp_path,
        wat,
        name=PLUGIN_ID,
        manifest_dict=fixtures.manifest(
            plugin_id=PLUGIN_ID,
            requested_capabilities=[capability],
            limits={"fuel": 500_000_000, **limits},
        ),
    )


def _host(
    runtime: Runtime,
    *capabilities,
    executor: HostExecutor | None = None,
    **executor_kwargs: int | float,
) -> WasmPluginHost:
    registry = HostCapabilityRegistry(permission_checker=runtime.permissions, capabilities={})
    for cap in capabilities:
        registry.register(cap)
    return WasmPluginHost(registry=registry, executor=executor or HostExecutor(**executor_kwargs))


async def _grant(runtime: Runtime, capability: str) -> None:
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN_ID),
            capability=capability,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="execution-test",
    )


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# -- 1. the event loop stays responsive ---------------------------------------


async def test_a_blocking_capability_does_not_freeze_the_event_loop(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The Phase 2.3 defect, measured directly.

    Before this phase the capability ran on the loop thread, so a 400 ms
    capability meant 400 ms of no scheduling. A heartbeat is the cheapest honest
    witness: it ticks only if the loop gets control.
    """
    cap = caps.BlockingCapability(delay=0.4)
    host = _host(runtime, cap, blocking_workers=2)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await host.invoke(host.load(path), {})
    finally:
        beat.cancel()

    assert result.ok is True, result.error
    assert cap.finished_at is not None, "the capability never ran"
    # ~40 ticks are available in 400 ms; 15 leaves room for a loaded machine
    # without accepting a frozen loop.
    assert ticks >= 15, f"the loop ticked only {ticks} times while the capability blocked"
    await host.aclose()


# -- 2. an async capability really is cancelled -------------------------------


async def test_an_async_capability_confirms_its_cancellation(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Task.cancel()`` works on a coroutine, so the state must be ``cancelled``."""
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.AsyncCapability(delay=1.0)
    host = _host(runtime, cap, cancellation_grace_s=0.3)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    call = result.host_calls[0]
    assert call.state == "cancelled"
    assert call.confirmed_stopped is True
    assert call.status == -10
    assert cap.cancelled_at is not None, "the coroutine never saw CancelledError"
    assert cap.finished_at is None, "the work completed; it was not cancelled"
    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    await host.aclose()


# -- 3. a sync capability times out without pretending to stop ----------------


async def test_a_sync_capability_times_out_and_keeps_running(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.BlockingCapability(delay=0.3)
    host = _host(runtime, cap)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    call = result.host_calls[0]
    assert call.state == "timed_out"
    assert call.confirmed_stopped is False, "a timeout was recorded as a stop"
    assert cap.finished_at is None, "the capability was faster than the timeout; test is moot"
    assert host.executor.outstanding() == 1, "the slot was released while the work ran"

    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    assert cap.finished_at is not None
    await host.aclose()


# -- 4. an unfinished operation keeps its slot ---------------------------------


async def test_an_unfinished_operation_still_holds_its_slot(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover operation must count against the plugin on the next call.

    Without this, a plugin could time out 64 calls and leave 64 threads behind
    while every subsequent call looked free.

    The queue is disabled here on purpose, so the refusal is immediate and this
    test measures the slot, not the wait.
    """
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.BlockingCapability(delay=0.5)
    host = _host(runtime, cap, max_outstanding_per_plugin=1, blocking_workers=2, max_waiters=0)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name, max_host_calls=4)
    await _grant(runtime, cap.name)
    plugin = host.load(path)

    first = await host.invoke(plugin, {})
    assert first.host_calls[0].state == "timed_out"
    assert host.executor.outstanding(PLUGIN_ID) == 1

    second = await host.invoke(plugin, {})
    refused = second.host_calls[0]
    assert refused.state == "refused"
    assert refused.decision == "concurrency"
    assert refused.status == -9
    assert refused.cost == 0, "a refused call must not be charged"
    assert cap.runs == 1, "the second call executed anyway"

    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    await host.aclose()


# -- 5. a host may decline to queue -------------------------------------------


async def test_a_host_without_a_queue_refuses_instead_of_backlogging(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``max_waiters=0`` a full pool refuses at once, and says so.

    Queueing is the default in Phase 2.5, but it is a policy a host can decline:
    the same saturation then produces an immediate refusal rather than a wait.
    Both behaviours are tested so neither becomes accidental.
    """
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.BlockingCapability(delay=0.4)
    host = _host(
        runtime,
        cap,
        blocking_workers=1,
        max_concurrent=2,
        max_outstanding_per_plugin=4,
        max_concurrent_per_invocation=4,
        max_waiters=0,
    )
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    # One leftover blocking operation fills the pool.
    first = await host.invoke(host.load(path), {})
    assert first.host_calls[0].state == "timed_out"
    assert host.executor.snapshot()["blocking"] == 1

    second = await host.invoke(host.load(path), {})
    refused = second.host_calls[0]
    assert refused.decision == "concurrency"
    assert refused.status == -9
    assert cap.runs == 1, "the pool limit did not actually prevent execution"
    assert host.executor.snapshot()["blocking"] == 1, "a refused call took a slot"

    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    await host.aclose()


# -- 6. cost budget is charged before execution -------------------------------


async def test_the_cost_budget_stops_an_expensive_capability_before_it_runs(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``max_cost`` must refuse the call, not refuse the *result*."""
    cap = caps.BlockingCapability(name="test.costly", delay=0.0, cost=10)
    host = _host(runtime, cap)
    path = _plugin(
        tmp_path,
        fixtures.repeated_host_calls(3, _request(cap.name)),
        cap.name,
        max_cost=15,
        max_host_calls=8,
    )
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginResourceError.code
    assert result.error.details["state"] == "refused"
    assert result.error.details["cost_budget"] == 15
    assert result.error.details["cost_spent"] == 10

    statuses = [c.status for c in result.host_calls]
    assert statuses[0] >= 0
    assert statuses[1:] == [-8, -8], statuses
    assert [c.decision for c in result.host_calls] == ["allow", "cost", "cost"]
    # The whole point: the third and fourth request never reached the capability.
    assert cap.runs == 1
    assert result.cost_used == 10
    assert result.cost_budget == 15
    await host.aclose()


async def test_a_refused_call_is_never_executed_and_never_charged(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Repeating a request cannot buy a cheaper admission.

    Ten calls at cost 10 against a budget of 25 buys exactly two executions. If
    refusals were charged, the second plugin call would fail; if refusals were
    free *and* admitted, ten would run.
    """
    cap = caps.BlockingCapability(name="test.costly", delay=0.0, cost=10)
    host = _host(runtime, cap)
    path = _plugin(
        tmp_path,
        fixtures.repeated_host_calls(10, _request(cap.name)),
        cap.name,
        max_cost=25,
        max_host_calls=32,
    )
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert cap.runs == 2, f"expected 2 executions, got {cap.runs}"
    assert result.cost_used == 20, "a refused call was charged"
    refusals = [c for c in result.host_calls if c.state == "refused"]
    assert len(refusals) == 8
    assert all(c.cost == 0 for c in refusals)
    await host.aclose()


# -- 7. a failure is admitted, executed and audited ---------------------------


async def test_a_capability_failure_is_audited_as_admitted_but_failed(
    runtime: Runtime, tmp_path: Path
) -> None:
    cap = caps.RaisingCapability()
    host = _host(runtime, cap)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginHostError.code
    call = result.host_calls[0]
    assert call.state == "failed"
    assert call.admitted is True, "it was authorised and admitted"
    assert call.executed is True, "it ran and raised"
    assert call.decision == "error"
    assert "sk-live" not in json.dumps(result.error.model_dump(mode="json"))
    assert await _wait_until(lambda: host.executor.outstanding() == 0), "the slot leaked"
    await host.aclose()


# -- 8. cancelling the invocation does not lie about the capability -----------


async def test_cancelling_the_invocation_stops_an_async_capability(
    runtime: Runtime, tmp_path: Path
) -> None:
    cap = caps.AsyncCapability(delay=5.0)
    host = _host(runtime, cap, cancellation_grace_s=0.3)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)
    plugin = host.load(path)

    task = asyncio.create_task(host.invoke(plugin, {}))
    assert await _wait_until(lambda: cap.started_at is not None)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _wait_until(lambda: cap.cancelled_at is not None), (
        "the cancellation never reached the capability"
    )
    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    await host.aclose()


async def test_cancelling_the_invocation_does_not_claim_a_sync_capability_stopped(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The honest half: we cannot stop it, so the counter must still show it.

    An implementation that decremented on cancellation would report zero here and
    be wrong. This is the assertion that keeps ``abort_scope`` from becoming a
    reassuring fiction.
    """
    cap = caps.BlockingCapability(delay=0.6)
    host = _host(runtime, cap, cancellation_grace_s=0.05)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)
    plugin = host.load(path)

    task = asyncio.create_task(host.invoke(plugin, {}))
    assert await _wait_until(lambda: cap.running == 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cap.finished_at is None
    assert host.executor.outstanding(PLUGIN_ID) == 1, (
        "a cancelled invocation reported its still-running work as finished"
    )
    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    await host.aclose()


# -- 9. a stubborn coroutine is not reported as cancelled ---------------------


async def test_a_capability_that_ignores_cancellation_is_reported_honestly(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cancellation_requested`` exists for exactly this capability.

    It swallows ``CancelledError`` and keeps working. Reporting ``cancelled``
    would be a lie that only shows up later, when the "stopped" operation writes
    something.
    """
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.StubbornAsyncCapability(delay=0.4, extra=0.3)
    host = _host(runtime, cap, cancellation_grace_s=0.05)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name, timeout_ms=10_000)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    call = result.host_calls[0]
    assert result.ok is False
    assert call.state == "cancellation_requested"
    assert call.confirmed_stopped is False
    assert call.status == -11
    assert cap.saw_cancel is True, "the cancel was never delivered; this proves nothing"
    assert cap.finished_at is None, "it stopped after all; the state was wrong"
    assert host.executor.outstanding() == 1

    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    assert cap.finished_at is not None, "it never finished; the release was premature"
    await host.aclose()


# -- 10. reading a timeout twice does not run the work twice ------------------


async def test_a_timed_out_call_is_executed_and_charged_exactly_once(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.BlockingCapability(delay=0.2)
    host = _host(runtime, cap)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})

    first_read = len(result.host_calls)
    second_read = len(result.host_calls)
    assert first_read == second_read == 1
    assert result.cost_used == 1

    assert await _wait_until(lambda: host.executor.outstanding() == 0)
    # The capability finishing later must not re-enter the audit, re-charge, or
    # resurrect the invocation.
    assert cap.runs == 1
    assert len(result.host_calls) == 1
    assert result.cost_used == 1
    await host.aclose()


# -- 11. budgets are per invocation, not per plugin or per host ---------------


async def test_two_plugins_do_not_share_one_invocation_budget(
    runtime: Runtime, tmp_path: Path
) -> None:
    cap = caps.BlockingCapability(name="test.costly", delay=0.0, cost=10)
    host = _host(runtime, cap)
    limit = {"max_cost": 10, "max_host_calls": 4}

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    for directory, plugin_id in ((first_dir, "alpha"), (second_dir, "beta")):
        fixtures.write_plugin(
            directory,
            fixtures.host_request(_request(cap.name)),
            name=plugin_id,
            manifest_dict=fixtures.manifest(
                plugin_id=plugin_id,
                requested_capabilities=[cap.name],
                limits={"fuel": 500_000_000, **limit},
            ),
        )
        await runtime.permissions.grant(
            PermissionRequest(
                adapter_id=principal_for(plugin_id),
                capability=cap.name,
                scope=PermissionScope.PLUGIN_HOST_CALL,
                risk=RiskLevel.LOW,
            ),
            granted_by="execution-test",
        )

    first = await host.invoke(host.load(first_dir / "alpha.wasm"), {})
    second = await host.invoke(host.load(second_dir / "beta.wasm"), {})

    assert first.ok is True, first.error
    assert second.ok is True, second.error
    assert first.cost_used == 10
    assert second.cost_used == 10, "the second plugin inherited the first one's bill"
    assert cap.runs == 2
    await host.aclose()


# -- 12. shutdown policy is explicit and verifiable ---------------------------


async def test_shutdown_reports_work_it_left_running_instead_of_waiting(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aclose()`` must not block on work it already gave up on.

    The returned count is the whole point: it converts "we shut down" into
    "we shut down and left one operation running", which is a fact an operator can
    act on.
    """
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    cap = caps.BlockingCapability(delay=0.6)
    host = _host(runtime, cap)
    path = _plugin(tmp_path, fixtures.host_request(_request(cap.name)), cap.name)
    await _grant(runtime, cap.name)

    result = await host.invoke(host.load(path), {})
    assert result.host_calls[0].state == "timed_out"
    assert host.executor.outstanding() == 1

    started = time.perf_counter()
    leftover = await host.aclose()
    elapsed = time.perf_counter() - started

    assert leftover == 1, "shutdown under-reported what it left behind"
    assert elapsed < 0.3, f"shutdown waited {elapsed:.2f}s for work it cannot stop"
    # The operation still finishes; shutdown simply stopped owning it.
    assert await _wait_until(lambda: cap.finished_at is not None)
