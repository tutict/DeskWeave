"""Approval consistency: the ways an approval could stop being the truth.

Phase 2.6 shipped an approval boundary and a revocation that reports honestly.
Phase 2.6.1 then measured four ways the *state* could still disagree with the
decision it records:

* an approval that only ever added grants, so narrowing it left the dropped
  capability granted while the record said it was not;
* a queued host call that was admitted after the approval was withdrawn;
* a second lifecycle that kept activating a build another process had revoked;
* a no-op reinstall that silently cleared a live approval.

Every test here runs the real permission store, the real executor and real WAT
modules. None of them uses ``sleep`` to make a race happen: the blocked
capability is released by a ``threading.Event`` that the test controls, and the
queue is observed through the executor's own counters, so a passing result means
the ordering was asserted rather than hoped for.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm.errors import PluginError
from deskweave_plugin_wasm.execution import HostExecutor
from deskweave_plugin_wasm.host import default_registry, principal_for
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    LifecycleEvent,
    PluginLifecycle,
)
from deskweave_plugin_wasm.runtime import WasmPlugin, WasmPluginHost
from deskweave_runtime import Runtime
from support import host_capabilities as caps
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
SECOND = "test.second"
PLUGIN = "lifecycle"


def _valid_payload() -> dict:
    return {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}}


def _seal(module: Path, **overrides: object) -> Path:
    """Rewrite the sibling manifest so it *describes* the bytes just written."""
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=module.stem, wasm_path=module.name, wasm_sha256=digest, **overrides
    )
    module.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    return module


def _module(
    tmp_path: Path,
    *,
    capability: str = VALID,
    payload: dict | None = None,
    requested: list[str] | None = None,
    version: str = "1.0.0",
    limits: dict[str, Any] | None = None,
    name: str = PLUGIN,
) -> Path:
    """A module that calls ``capability`` once, with ``payload`` baked in.

    The payload is part of the *bytes*, which is how a second build of the same
    version is produced without a compiler: change the payload, change the
    digest.
    """
    request = json.dumps({"capability": capability, "payload": payload or _valid_payload()})
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=name, manifest_dict={}
    )
    overrides: dict[str, Any] = {
        "version": version,
        "requested_capabilities": requested if requested is not None else [capability],
        # Authorisation is what these tests are about; payload shape is covered
        # by `test_wasm_plugin_negative`.
        "input_schema": {},
        "output_schema": {},
    }
    if limits is not None:
        overrides["limits"] = limits
    return _seal(module, **overrides)


def _lifecycle(
    runtime: Runtime,
    *,
    store: Any = None,
    extra: dict[str, Any] | None = None,
    executor: HostExecutor | None = None,
) -> PluginLifecycle:
    registry = default_registry(runtime.permissions, executor)
    for capability in (extra or {}).values():
        registry.register(capability)
    return PluginLifecycle(registry=registry, permissions=runtime.permissions, store=store)


async def _approved(
    runtime: Runtime,
    tmp_path: Path,
    *,
    approve: list[str] | None = None,
    store: Any = None,
    extra: dict[str, Any] | None = None,
    **module_kwargs: object,
) -> tuple[PluginLifecycle, WasmPlugin, Path]:
    lifecycle = _lifecycle(runtime, store=store, extra=extra)
    module = _module(tmp_path, **module_kwargs)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, approve or [VALID])
    plugin = await lifecycle.activate(module)
    return lifecycle, plugin, module


async def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    """Poll a *counter* until it moves. Not a sleep: the predicate is the signal."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


class _GatedCapability:
    """Synchronous, and blocks until the test opens the gate.

    Deterministic by construction: the test waits on ``entered`` before deciding
    anything, so there is no interval in which the outcome depends on timing.
    """

    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    cost = 1
    supports_cancellation = False

    def __init__(self, name: str = SECOND) -> None:
        self.name = name
        self.entered = threading.Event()
        self.release = threading.Event()
        self.runs = 0

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs += 1
        self.entered.set()
        # A bound, not a delay: the test opens it as soon as it has asserted.
        self.release.wait(5.0)
        return {"ran": True}


# -- F4: reinstalling the same bytes is not a new decision --------------------


async def test_reinstalling_the_same_bytes_does_not_touch_the_record(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The whole store file is compared, so "nothing was written" is literal."""
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle, plugin, module = await _approved(runtime, tmp_path, store=store)
    before = store.path.read_text(encoding="utf-8")
    grants_before = await runtime.permissions.list_grants()

    _record, again = await lifecycle.install(module.with_suffix(".json"))

    assert store.path.read_text(encoding="utf-8") == before
    assert again.state is InstallState.ACTIVE
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.ACTIVE
    assert inspected.approved == (VALID,)
    assert inspected.granted == (VALID,)
    assert inspected.approval_in_force is True
    # No misleading "reinstalled" / "approved" event: the last real thing that
    # happened was the activation.
    assert inspected.last_event is LifecycleEvent.ACTIVATED
    assert await runtime.permissions.list_grants() == grants_before

    # And the capability still runs under the approval that was never withdrawn.
    result = await lifecycle.host.invoke(plugin, {"images": [_valid_payload()["image"]]})
    assert result.ok is True, result.error


async def test_a_different_build_is_not_short_circuited(runtime: Runtime, tmp_path: Path) -> None:
    lifecycle, _plugin, _module_path = await _approved(runtime, tmp_path)
    # Same version, same capability, different bytes: the payload is embedded in
    # the module, so this is genuinely a second build.
    rebuilt = _module(
        tmp_path,
        payload={"image": {"artifact_id": "b", "title": "t", "width": 2, "height": 2}},
    )

    await lifecycle.install(rebuilt.with_suffix(".json"), replace=True)

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.INSTALLED
    assert inspected.approved == ()
    assert inspected.approval_in_force is False
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(rebuilt)
    assert caught.value.code == "plugin_not_approved"


async def test_a_new_capability_set_is_not_short_circuited(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, _plugin, _module_path = await _approved(runtime, tmp_path)
    grown = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])

    await lifecycle.install(grown.with_suffix(".json"), replace=True)

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.INSTALLED
    assert inspected.requested == (VALID, SECOND)
    assert inspected.approved == ()


# -- F1: an approval is a set, not a growing list -----------------------------


async def test_narrowing_an_approval_revokes_the_grant_it_dropped(
    runtime: Runtime, tmp_path: Path
) -> None:
    second = caps.BlockingCapability(SECOND)
    lifecycle = _lifecycle(runtime, extra={"second": second})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID, SECOND])
    assert (await lifecycle.inspect(PLUGIN)).granted == (VALID, SECOND)

    await lifecycle.approve(PLUGIN, [VALID])

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.approved == (VALID,)
    assert inspected.granted == (VALID,)
    assert inspected.approval_in_force is True

    plugin = await lifecycle.activate(module)
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is False
    assert result.error.code == "plugin_capability_denied"
    assert result.host_calls[0].executed is False
    assert second.runs == 0


async def test_narrowing_after_a_revocation_grants_only_the_new_set(
    runtime: Runtime, tmp_path: Path
) -> None:
    second = caps.BlockingCapability(SECOND)
    lifecycle, _plugin, module = await _approved(
        runtime,
        tmp_path,
        approve=[VALID, SECOND],
        extra={"second": second},
        capability=SECOND,
        requested=[VALID, SECOND],
    )
    await lifecycle.revoke(PLUGIN)
    assert (await lifecycle.inspect(PLUGIN)).granted == ()

    await lifecycle.approve(PLUGIN, [VALID])
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.approved == (VALID,)
    assert inspected.granted == (VALID,)

    plugin = await lifecycle.activate(module)
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is False
    assert second.runs == 0


async def test_a_failed_revoke_never_saves_the_new_approval(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dangerous half of an approval must not be persisted on its own."""
    lifecycle = _lifecycle(runtime, extra={"second": caps.BlockingCapability(SECOND)})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID, SECOND])

    async def broken_revoke(adapter_id, capabilities=None):
        raise RuntimeError("the grant store is unreachable")

    monkeypatch.setattr(runtime.permissions, "revoke", broken_revoke)
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve(PLUGIN, [VALID])

    assert caught.value.details["approval_saved"] is False
    inspected = await lifecycle.inspect(PLUGIN)
    # The record still says what was true before the attempt.
    assert inspected.approved == (VALID, SECOND)
    assert inspected.granted == (VALID, SECOND)


# -- F10: history is not authority -------------------------------------------


async def test_inspect_marks_a_revoked_approval_as_historical(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, _plugin, _module_path = await _approved(runtime, tmp_path)
    await lifecycle.revoke(PLUGIN)

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.REVOKED
    # The approval is still reported, because deleting history is not the same as
    # withdrawing authority — but it authorises nothing.
    assert inspected.approved == (VALID,)
    assert inspected.approval_in_force is False
    assert inspected.drift is not None and inspected.drift.ok is True
    assert any("historical" in note for note in inspected.notes)


# -- F3: a second lifecycle must not act on a cache ---------------------------


async def test_a_second_lifecycle_cannot_activate_after_a_revocation(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = tmp_path / "approvals.json"
    first = _lifecycle(runtime, store=JsonApprovalStore(path))
    second = _lifecycle(runtime, store=JsonApprovalStore(path))
    module = _module(tmp_path)
    await first.discover(tmp_path)
    await first.install(module.with_suffix(".json"))
    await first.approve(PLUGIN, [VALID])
    plugin = await first.activate(module)

    # The second lifecycle now holds a snapshot that says "approved".
    assert (await second.inspect(PLUGIN)).state is InstallState.ACTIVE

    await first.revoke(PLUGIN)

    with pytest.raises(PluginError) as caught:
        await second.activate(plugin.path)
    assert caught.value.code == "plugin_not_approved"
    assert (await second.inspect(PLUGIN)).state is InstallState.REVOKED


async def test_the_store_is_read_rather_than_sampled(runtime: Runtime, tmp_path: Path) -> None:
    """Backdating the file proves the check is on content, not on ``mtime``."""
    path = tmp_path / "approvals.json"
    writer = _lifecycle(runtime, store=JsonApprovalStore(path))
    reader = _lifecycle(runtime, store=JsonApprovalStore(path))
    module = _module(tmp_path)
    await writer.discover(tmp_path)
    await writer.install(module.with_suffix(".json"))
    await writer.approve(PLUGIN, [VALID])
    plugin = await writer.activate(module)
    assert (await reader.inspect(PLUGIN)).state is InstallState.ACTIVE

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["plugins"][PLUGIN]["state"] = "revoked"
    raw["plugins"][PLUGIN]["approval"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    stale = path.stat().st_mtime - 3_600
    os.utime(path, (stale, stale))

    with pytest.raises(PluginError) as caught:
        await reader.activate(plugin.path)
    assert caught.value.code == "plugin_not_approved"


async def test_two_lifecycles_never_leave_grants_that_disagree_with_the_record(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Concurrent approvals are serialised; whichever wins, the pair matches."""
    path = tmp_path / "approvals.json"
    left = _lifecycle(
        runtime, store=JsonApprovalStore(path), extra={"second": caps.BlockingCapability(SECOND)}
    )
    right = _lifecycle(
        runtime, store=JsonApprovalStore(path), extra={"second": caps.BlockingCapability(SECOND)}
    )
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await left.discover(tmp_path)
    await left.install(module.with_suffix(".json"))

    await asyncio.gather(
        left.approve(PLUGIN, [VALID]),
        right.approve(PLUGIN, [VALID, SECOND]),
    )

    inspected = await left.inspect(PLUGIN)
    assert inspected.approved == inspected.granted
    assert inspected.approved in {(VALID,), (VALID, SECOND)}


# -- F5: uninstall leaves a tombstone, not a gap ------------------------------


async def test_uninstall_keeps_a_tombstone_instead_of_deleting_the_record(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = tmp_path / "approvals.json"
    store = JsonApprovalStore(path)
    lifecycle, _plugin, _module_path = await _approved(runtime, tmp_path, store=store)

    await lifecycle.uninstall(PLUGIN)

    raw = json.loads(path.read_text(encoding="utf-8"))
    entry = raw["plugins"][PLUGIN]
    assert entry["state"] == "uninstalled"
    assert entry["approval"] is None
    assert entry["previous_approval"] is None
    assert entry["capabilities"] == []

    # A fresh process can tell "uninstalled here" apart from "never seen here".
    reopened = _lifecycle(runtime, store=JsonApprovalStore(path))
    inspected = await reopened.inspect(PLUGIN)
    assert inspected.state is InstallState.UNINSTALLED
    assert inspected.approval_in_force is False
    assert any("tombstone" in note for note in inspected.notes)


# -- F2: revocation is linearised at admission -------------------------------


async def test_a_queued_call_never_runs_after_the_approval_is_withdrawn(
    runtime: Runtime, tmp_path: Path
) -> None:
    gate = _GatedCapability(SECOND)
    executor = HostExecutor(
        max_concurrent=4,
        blocking_workers=2,
        max_outstanding_per_plugin=1,
    )
    lifecycle = _lifecycle(runtime, extra={"gate": gate}, executor=executor)
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [SECOND])

    # Two *distinct* plugin objects: one WasmPlugin serialises its own calls, so a
    # second concurrent call has to come from a second instance, exactly as it
    # would from a second host process. They come from `activate()` rather than
    # `host.load()` because Phase 2.6.4 F2 made a bare load require the approval
    # snapshot that only `activate()` can take; the subject here is revocation at
    # admission, not loading.
    first = await lifecycle.activate(module, module.with_suffix(".json"))
    second = await lifecycle.activate(module, module.with_suffix(".json"))

    running = asyncio.create_task(lifecycle.host.invoke(first, {"images": []}))
    # Deterministic: the capability tells us it has started.
    assert await asyncio.to_thread(gate.entered.wait, 5.0), "the first call never started"
    assert executor.outstanding(PLUGIN) == 1

    queued = asyncio.create_task(lifecycle.host.invoke(second, {"images": []}))
    assert await _wait_until(lambda: executor.queued_for(PLUGIN) == 1), (
        "the second call was not parked in the admission queue"
    )

    report = await lifecycle.revoke(PLUGIN)
    assert report.queued_stopped == 1
    assert report.running == 1
    assert report.still_running == 1
    assert report.stopped is False

    gate.release.set()
    first_result = await running
    queued_result = await queued

    assert first_result.ok is True
    assert queued_result.ok is False
    assert queued_result.host_calls[0].decision == "revoked"
    assert queued_result.host_calls[0].executed is False
    assert gate.runs == 1


async def test_a_revocation_that_cannot_be_recorded_does_not_report_success(
    runtime: Runtime, tmp_path: Path
) -> None:
    class _Unwritable(JsonApprovalStore):
        fail: bool = False

        async def save(self, record):  # type: ignore[override]
            if self.fail:
                raise OSError("the approval store is full")
            return await super().save(record)

    store = _Unwritable(tmp_path / "approvals.json")
    capability = caps.BlockingCapability(SECOND, delay=0.01)
    lifecycle = _lifecycle(runtime, store=store, extra={"second": capability})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [SECOND])
    plugin = await lifecycle.activate(module)

    store.fail = True
    with pytest.raises(PluginError) as caught:
        await lifecycle.revoke(PLUGIN)
    assert caught.value.details["revocation_persisted"] is False
    # Nothing was half-applied: the record, the grants and the executor's view all
    # still describe a plugin that is approved and runnable.
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.ACTIVE
    assert inspected.granted == (SECOND,)
    assert lifecycle.host.executor.is_revoked(PLUGIN) is False
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is True, result.error
    assert capability.runs == 1


# -- F9: a grant is not an approval ------------------------------------------


async def test_a_hand_written_grant_does_not_authorise_a_plugin_call(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The permission layer would honour this grant; the approval never covered it."""
    lifecycle, _plugin, _module = await _approved(
        runtime,
        tmp_path,
        approve=[VALID],
        capability=SECOND,
        requested=[VALID, SECOND],
        extra={"second": caps.BlockingCapability(SECOND)},
    )
    handler = lifecycle.registry.capabilities[SECOND]
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN),
            capability=SECOND,
            scope=handler.scope,
            risk=handler.risk,
            resource=PLUGIN,
        ),
        granted_by="hand-written",
    )
    # The grant is real, and the permission layer will say ALLOW for it.
    allowed = await runtime.permissions.check(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN),
            capability=SECOND,
            scope=handler.scope,
            risk=handler.risk,
        )
    )
    assert allowed.decision.value == "allow"

    # `_plugin` is the object `activate()` already returned; Phase 2.6.4 F2 means
    # a second bare `host.load(...)` is refused (no approval snapshot), and this
    # test is about the *call*, not about loading twice.
    plugin = _plugin
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is False
    assert result.host_calls[0].decision == "not_approved"
    assert result.host_calls[0].executed is False
    assert lifecycle.registry.capabilities[SECOND].runs == 0


# -- F6: a refusal must not spend a one-shot ---------------------------------


async def test_a_cost_refusal_does_not_spend_the_one_shot(runtime: Runtime, tmp_path: Path) -> None:
    expensive = caps.BlockingCapability(SECOND, cost=2)
    registry = default_registry(runtime.permissions)
    registry.register(expensive)
    host = WasmPluginHost(registry=registry)
    module = _module(
        tmp_path,
        capability=SECOND,
        requested=[SECOND],
        limits={"max_cost": 1},
    )
    plugin = host.load(module)

    runtime.permissions.allow_once(principal_for(PLUGIN), SECOND)
    first = await host.invoke(plugin, {"images": []})
    assert first.ok is False
    assert first.host_calls[0].decision == "cost"
    assert expensive.runs == 0

    # The allowance was handed back, so the *same* refusal happens again instead
    # of degrading to "ask the user again".
    second = await host.invoke(plugin, {"images": []})
    assert second.ok is False
    assert second.host_calls[0].decision == "cost"
    assert expensive.runs == 0


# -- F7: every refusal says what it was --------------------------------------


async def test_a_budget_refusal_records_a_state(runtime: Runtime, tmp_path: Path) -> None:
    registry = default_registry(runtime.permissions)
    registry.register(caps.BlockingCapability(SECOND))
    host = WasmPluginHost(registry=registry)
    request = json.dumps({"capability": SECOND, "payload": {}})
    module = fixtures.write_plugin(
        tmp_path,
        fixtures.repeated_host_calls(2, request),
        name=PLUGIN,
        manifest_dict=fixtures.manifest(plugin_id=PLUGIN, requested_capabilities=[SECOND]),
    )
    _seal(module, requested_capabilities=[SECOND], limits={"max_host_calls": 1}, input_schema={})
    plugin = host.load(module)
    runtime.permissions.allow_once(principal_for(PLUGIN), SECOND)

    result = await host.invoke(plugin, {})
    budget = [call for call in result.host_calls if call.decision == "budget"]
    assert budget, "the budget refusal was not recorded"
    assert budget[0].state == "refused"


async def test_a_request_refused_before_dispatch_records_a_state(
    runtime: Runtime, tmp_path: Path
) -> None:
    registry = default_registry(runtime.permissions)
    registry.register(caps.BlockingCapability(SECOND))
    host = WasmPluginHost(registry=registry)
    request = json.dumps({"capability": SECOND, "payload": {}})
    module = fixtures.write_plugin(
        tmp_path,
        fixtures.host_request_bad_req_ptr(request),
        name=PLUGIN,
        manifest_dict=fixtures.manifest(plugin_id=PLUGIN, requested_capabilities=[SECOND]),
    )
    _seal(module, requested_capabilities=[SECOND], input_schema={})
    plugin = host.load(module)

    result = await host.invoke(plugin, {})
    assert result.host_calls
    assert result.host_calls[0].state == "refused"
    assert result.host_calls[0].executed is False


# -- F8: a strict host without an approval check cannot be built by accident --


def test_a_pinning_host_without_an_approval_guard_is_refused() -> None:
    with pytest.raises(PluginError) as caught:
        WasmPluginHost(require_install_record=True)
    assert caught.value.code == "plugin_lifecycle"
    assert "activation_guard" in caught.value.message
    # Saying it out loud is allowed: the integrity layer has its own tests.
    assert WasmPluginHost(require_install_record=True, allow_unguarded=True) is not None


# -- F12: a guard that explodes still refuses and still records --------------


async def test_an_activation_guard_crash_is_recorded_and_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, _plugin, module = await _approved(runtime, tmp_path)

    def explode(manifest, digest, records):  # the 3-arg protocol; never called with meaning
        raise RuntimeError("the guard exploded")

    lifecycle.host._activation_guard = explode  # type: ignore[attr-defined]

    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_not_approved"
    assert caught.value.details["error"] == "RuntimeError"

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.last_event is LifecycleEvent.ACTIVATION_REFUSED
    assert "RuntimeError" in inspected.last_detail["reason"]
