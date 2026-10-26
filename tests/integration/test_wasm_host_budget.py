"""Tests: what a host call costs, and what happens when the budget ends.

Two budgets are deliberately kept apart:

* **fuel** bounds guest instructions. It is deterministic and it does not care
  what the guest spends them on.
* **host calls** bound how many times the guest can make the *host* do work. One
  host call costs the guest almost no fuel while costing the host a permission
  check, a JSON round trip and whatever the capability itself performs.

Conflating them is the mistake this file exists to prevent: a plugin with a
generous fuel budget and an unlimited host-call allowance can make the host
launch a browser ten thousand times for the price of one loop.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import ClassVar

import pytest

import deskweave_plugin_wasm.runtime as runtime_module
from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    HostCapabilityRegistry,
    PluginHostError,
    PluginResourceError,
    WasmPluginHost,
    principal_for,
)
from deskweave_plugin_wasm.host import ReferenceMetadataValidate
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

CAPABILITY = "reference.metadata.validate"
PLUGIN_ID = "budgeted"
IMAGE = {"artifact_id": "art-1", "title": "Cottage", "width": 800, "height": 600}
REQUEST = json.dumps({"capability": CAPABILITY, "payload": {"image": IMAGE}})


async def _grant(
    runtime: Runtime, capability: str = CAPABILITY, plugin_id: str = PLUGIN_ID
) -> None:
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(plugin_id),
            capability=capability,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="budget-test",
    )


def _plugin(tmp_path: Path, wat: str, **limits: int) -> Path:
    return fixtures.write_plugin(
        tmp_path,
        wat,
        name=PLUGIN_ID,
        manifest_dict=fixtures.manifest(
            plugin_id=PLUGIN_ID,
            requested_capabilities=[CAPABILITY],
            limits={"fuel": 500_000_000, **limits},
        ),
    )


def _host(runtime: Runtime, registry: HostCapabilityRegistry | None = None) -> WasmPluginHost:
    from deskweave_plugin_wasm import default_registry

    return WasmPluginHost(registry=registry or default_registry(runtime.permissions))


# -- the counter ---------------------------------------------------------------


async def test_budget_allows_exactly_the_declared_number(runtime: Runtime, tmp_path: Path) -> None:
    """N calls succeed; call N+1 is refused. Not N-1, not N+1."""
    path = _plugin(tmp_path, fixtures.repeated_host_calls(5, REQUEST), max_host_calls=5)
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is True, result.error
    assert len(result.host_calls) == 5
    assert all(call.ok for call in result.host_calls)


async def test_the_call_after_the_budget_fails_the_invocation(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = _plugin(tmp_path, fixtures.repeated_host_calls(6, REQUEST), max_host_calls=5)
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginResourceError.code
    assert result.error.details["budget"] == 5
    # Five dispatches plus exactly one budget record.
    assert len(result.host_calls) == 6
    assert result.host_calls[-1].decision == "budget"


async def test_budget_record_is_written_once_however_many_calls_follow(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Post-budget calls must not grow host memory through the audit trail."""
    path = _plugin(tmp_path, fixtures.repeated_host_calls(300, REQUEST), max_host_calls=3)
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert len(result.host_calls) == 4  # 3 dispatches + 1 budget marker, not 300


async def test_malformed_calls_consume_the_same_budget(runtime: Runtime, tmp_path: Path) -> None:
    """Counting dispatches instead of attempts would leave a free DoS channel.

    Every call below points the request pointer outside linear memory, so none is
    ever dispatched — yet each still costs the host a read attempt and an audit
    record. With a budget of 3 the audit must stop growing at 4 entries (three
    refusals plus one budget marker), not run to 20.
    """
    path = _plugin(tmp_path, fixtures.repeated_refused_calls(20), max_host_calls=3)
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert len(result.host_calls) == 4, [c.status for c in result.host_calls]
    assert result.host_calls[-1].decision == "budget"
    assert result.error is not None


async def test_budget_is_per_invocation_not_per_plugin(runtime: Runtime, tmp_path: Path) -> None:
    path = _plugin(tmp_path, fixtures.repeated_host_calls(2, REQUEST), max_host_calls=2)
    await _grant(runtime)
    host = _host(runtime)
    plugin = host.load(path)

    first = await host.invoke(plugin, {})
    second = await host.invoke(plugin, {})

    assert first.ok is True, first.error
    assert second.ok is True, second.error


async def test_budget_ceiling_cannot_be_raised_by_the_manifest(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A manifest asking for more than the host ceiling is a validation error."""
    from pydantic import ValidationError

    from deskweave_plugin_wasm.manifest import MAX_HOST_CALLS, ResourceLimits

    with pytest.raises(ValidationError):
        ResourceLimits(max_host_calls=MAX_HOST_CALLS + 1)


# -- the capability itself fails ----------------------------------------------


class _RaisingCapability:
    name = "test.raises"
    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict] = {"type": "object"}

    def run(self, payload):
        raise RuntimeError("capability exploded with token=sk-live-secret")


class _SlowCapability:
    name = "test.slow"
    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict] = {"type": "object"}
    finished_at: ClassVar[float | None] = None

    def run(self, payload):
        time.sleep(0.25)
        _SlowCapability.finished_at = time.perf_counter()
        return {"late": True}


def _host_with(runtime: Runtime, capability) -> WasmPluginHost:
    registry = HostCapabilityRegistry(permission_checker=runtime.permissions, capabilities={})
    registry.register(capability)
    registry.register(ReferenceMetadataValidate())
    return WasmPluginHost(registry=registry)


async def test_capability_exception_becomes_a_host_error_not_a_denial(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A capability bug must be reported as the host's fault, and must not leak."""
    request = json.dumps({"capability": "test.raises", "payload": {}})
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.host_request(request),
        name=PLUGIN_ID,
        manifest_dict=fixtures.manifest(
            plugin_id=PLUGIN_ID,
            requested_capabilities=["test.raises"],
            limits={"fuel": 500_000_000},
        ),
    )
    await _grant(runtime, capability="test.raises")

    result = await _host_with(runtime, _RaisingCapability()).invoke(
        _host_with(runtime, _RaisingCapability()).load(path), {}
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginHostError.code
    # The capability's own message — which contained a token — never crosses back.
    assert "sk-live" not in json.dumps(result.error.model_dump(mode="json"))
    assert "exploded" not in result.error.message
    assert result.host_calls[0].decision == "error"


async def test_host_call_timeout_is_contained_and_audited(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow capability must not escape ``invoke`` as a ``TimeoutError``."""
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    _SlowCapability.finished_at = None
    request = json.dumps({"capability": "test.slow", "payload": {}})
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.host_request(request),
        name=PLUGIN_ID,
        manifest_dict=fixtures.manifest(
            plugin_id=PLUGIN_ID,
            requested_capabilities=["test.slow"],
            limits={"fuel": 500_000_000},
        ),
    )
    await _grant(runtime, capability="test.slow")
    host = _host_with(runtime, _SlowCapability())

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginHostError.code
    # The refused call is still in the audit, with an honest reason.
    assert len(result.host_calls) == 1
    assert result.host_calls[0].status < 0
    assert "may still be running" in result.host_calls[0].reason


async def test_a_blocking_capability_no_longer_holds_the_caller(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2.4: the caller is released at the deadline, the work is not.

    Phase 2.3 recorded the opposite — the capability ran on the event loop, so a
    250 ms capability held the caller for 250 ms even with a 50 ms budget. Now it
    runs in the host's bounded pool, so three things must be true *at once*, and
    asserting them together is the point:

    1. the caller returns at roughly the deadline, not at the capability's speed;
    2. the capability has **not** finished at that moment (so nothing was
       accelerated, and no fake cancellation was reported);
    3. it does finish afterwards — it was timed out, not stopped.

    A test that only checked (1) would pass for an implementation that cancelled
    by throwing the result away. (2) and (3) are what make it evidence.
    """
    monkeypatch.setattr(runtime_module, "HOST_CALL_TIMEOUT_S", 0.05)
    _SlowCapability.finished_at = None
    request = json.dumps({"capability": "test.slow", "payload": {}})
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.host_request(request),
        name=PLUGIN_ID,
        manifest_dict=fixtures.manifest(
            plugin_id=PLUGIN_ID,
            requested_capabilities=["test.slow"],
            limits={"fuel": 500_000_000},
        ),
    )
    await _grant(runtime, capability="test.slow")
    host = _host_with(runtime, _SlowCapability())

    started = time.perf_counter()
    result = await host.invoke(host.load(path), {})
    elapsed = time.perf_counter() - started

    assert result.ok is False
    assert elapsed < 0.2, f"the caller waited {elapsed:.3f}s; the loop was blocked"
    assert _SlowCapability.finished_at is None, "the capability finished; wall clock proves nothing"
    call = result.host_calls[0]
    assert call.state == "timed_out"
    assert call.confirmed_stopped is False, "a timeout must never be recorded as a stop"

    # ... and it really does finish, so "timed out" was the honest state. The
    # slot is released by the completion callback, which runs after the function
    # returns, so this waits for the release rather than for the timestamp.
    deadline = time.perf_counter() + 3.0
    while host.executor.outstanding() and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)

    assert _SlowCapability.finished_at is not None, "the capability never ran to completion"
    assert host.executor.outstanding() == 0, "the slot outlived the work"
    await host.aclose()


async def test_an_unexpected_host_failure_is_contained(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment net is real: force an exception past every specific guard.

    Every guest-controlled path is supposed to be translated before it reaches
    here, so this test injects a failure that *cannot* be one of those paths. If
    the net is removed, ``invoke`` raises and the caller sees a crash instead of
    a failed invocation.
    """
    path = _plugin(tmp_path, fixtures.constant_response('{"ok": true}'))
    host = _host(runtime)

    def explode(*args: object, **kwargs: object) -> tuple[dict, int]:
        raise RuntimeError("simulated host defect")

    monkeypatch.setattr(WasmPluginHost, "_run", staticmethod(explode))
    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.details["host_exception"] == "RuntimeError"
    assert "simulated host defect" not in result.error.message


async def test_cancellation_of_the_invocation_is_not_swallowed(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``CancelledError`` must propagate: it is not a plugin failure."""
    path = _plugin(
        tmp_path,
        fixtures.spin_forever(),
        max_host_calls=1,
        fuel=5_000_000_000,
        timeout_ms=20_000,
    )
    host = _host(runtime)

    task = asyncio.create_task(host.invoke(host.load(path), {}))
    await asyncio.sleep(0.15)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
