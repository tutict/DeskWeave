"""Lifecycle integrity: the six corrections Phase 2.6.3 owes the 2.6.2 audit.

Each group is written against the *demonstrated* failure, so the test fails on
the 2.6.2 tree for the reason the audit gave:

* **A1** — an equivalent path spelling made an unchanged install look new, so a
  live approval was cleared while the call still reported ``unchanged``.
* **B1** — a guard that raised something other than ``PluginError`` escaped
  ``dispatch`` and was audited as ``admitted=True executed=True``.
* **B2** — a one-shot consumed by ``check`` was never returned when the approval
  guard refused the call, or by any path that did not reach ``conclude``.
* **C1/A2** — the revocation block lived inside one executor, so a call queued
  in a *second* host on the same store survived a revocation committed by the
  first.
* **D1** — ``list_grants`` had no expiry filter, so ``inspect`` and ``approve``
  both counted an expired row as current authority.
* **D2** — a store failure during ``approve`` escaped as a bare ``OSError`` with
  no structured result.

No test here uses ``sleep`` to make a race come out: the only waiting is on
``threading.Event`` gates, counters, and the executor's own queue depth.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm.abi import ABI_VERSION
from deskweave_plugin_wasm.errors import (
    PluginAbiMismatchError,
    PluginError,
    PluginLifecycleError,
    PluginNotApprovedError,
    plugin_error,
)
from deskweave_plugin_wasm.execution import ExecutionScope, HostExecutor
from deskweave_plugin_wasm.host import HostCapabilityRegistry, default_registry, principal_for
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    LifecycleRecord,
    MemoryApprovalStore,
    PluginLifecycle,
)
from deskweave_runtime import Runtime
from support import host_capabilities as caps
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
SECOND = "test.second"
PLUGIN = "lifecycle"


# -- fixtures ----------------------------------------------------------------


def _valid_payload() -> dict:
    return {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}}


def _seal(module: Path, **overrides: object) -> Path:
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
    name: str = PLUGIN,
) -> Path:
    """A module that calls ``capability`` once, with ``payload`` baked into the bytes."""
    request = json.dumps({"capability": capability, "payload": payload or _valid_payload()})
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=name, manifest_dict={}
    )
    return _seal(
        module,
        version=version,
        requested_capabilities=requested if requested is not None else [capability],
        input_schema={},
        output_schema={},
    )


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


async def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    """Poll a counter until it moves. The counter is the signal, not the clock."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


class _GatedCapability:
    """Synchronous capability that blocks until the test opens the gate."""

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
        self.release.wait(5.0)
        return {"ran": True}


class _ExplodingCapability:
    """Synchronous capability that raises, so the call is `failed` after starting."""

    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    cost = 1

    def __init__(self, name: str = SECOND) -> None:
        self.name = name
        self.runs = 0

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs += 1
        raise RuntimeError("the capability failed after it started")


class _FailingStore(JsonApprovalStore):
    """A store whose ``save`` fails on demand; reads keep working."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_saves = False

    async def save(self, record: LifecycleRecord) -> None:
        if self.fail_saves:
            raise OSError("the approval store is unreachable")
        await super().save(record)


def _registry(
    permissions,
    *,
    capability: Any = None,
    executor: HostExecutor | None = None,
    guard: Any = None,
) -> HostCapabilityRegistry:
    registry = HostCapabilityRegistry(
        capabilities={capability.name: capability} if capability is not None else {},
        permission_checker=permissions,
        executor=executor,
    )
    registry.dispatch_guard = guard
    return registry


class _GatedReadStore(JsonApprovalStore):
    """Reads the file, then blocks before handing the snapshot back.

    The window is deliberate: the snapshot is stale the moment it is taken, which
    is exactly what a revocation landing mid-guard looks like from the inside.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.gate = False
        self.entered = threading.Event()
        self.release = threading.Event()

    async def load_all(self):
        snapshot = await super().load_all()
        if self.gate:
            self.entered.set()
            await asyncio.get_running_loop().run_in_executor(None, self.release.wait, 5.0)
        return snapshot


# -- A1: an unchanged install stays unchanged however the path is spelled -----


async def test_a_relative_spelling_keeps_the_approval_granted_by_an_absolute_one(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store, extra={"second": caps.BlockingCapability(SECOND)})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID, SECOND])
    before = store.path.read_bytes()

    # Same directory, same file, relative spelling. Before the fix this looked
    # like a different installation and cleared the approval.
    monkeypatch.chdir(tmp_path)
    _record, saved = await lifecycle.install(Path(module.with_suffix(".json").name))

    inspected = await lifecycle.inspect(PLUGIN)
    assert saved.state is InstallState.APPROVED
    assert inspected.approved == (VALID, SECOND)
    assert inspected.granted == (VALID, SECOND)
    assert store.path.read_bytes() == before


async def test_an_absolute_spelling_keeps_the_approval_granted_by_a_relative_one(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)

    monkeypatch.chdir(tmp_path)
    await lifecycle.install(Path(module.with_suffix(".json").name))
    await lifecycle.approve(PLUGIN, [VALID])
    before = store.path.read_bytes()

    _record, saved = await lifecycle.install(module.with_suffix(".json"))

    assert saved.state is InstallState.APPROVED
    assert (await lifecycle.inspect(PLUGIN)).approved == (VALID,)
    assert store.path.read_bytes() == before


async def test_the_same_path_and_bytes_leave_the_store_byte_identical(
    runtime: Runtime, tmp_path: Path
) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])
    before = store.path.read_bytes()
    grants_before = await runtime.permissions.list_grants()

    _record, saved = await lifecycle.install(module.with_suffix(".json"))

    assert store.path.read_bytes() == before
    assert await runtime.permissions.list_grants() == grants_before
    assert saved.state is InstallState.APPROVED


async def test_a_record_that_disagrees_with_the_pin_takes_the_full_path(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The pin can say "unchanged" while the *manifest* describes something else.

    ``interface_version`` is absent from the pin's own ``unchanged`` test, so it
    is the one field that can move while ``integrity.install`` still returns
    ``unchanged`` — and the pin then keeps describing the old ABI. Comparing the
    record against the pin therefore compared two stale copies of the same fact
    and agreed with itself. The record has to be compared against the manifest
    that is on disk, or a stale record is kept forever.
    """
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    _seal(module, version="1.0.0", requested_capabilities=[VALID], interface_version=2)
    _record, saved = await lifecycle.install(module.with_suffix(".json"))

    assert saved.state is InstallState.INSTALLED
    assert saved.interface_version == 2
    assert (await lifecycle.inspect(PLUGIN)).approved == ()


async def test_a_changed_version_takes_the_full_path(runtime: Runtime, tmp_path: Path) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    _seal(module, version="1.0.1", requested_capabilities=[VALID])
    _record, saved = await lifecycle.install(module.with_suffix(".json"), replace=True)

    assert saved.state is InstallState.INSTALLED
    assert saved.version == "1.0.1"
    assert (await lifecycle.inspect(PLUGIN)).approved == ()


async def test_a_changed_requested_set_takes_the_full_path(
    runtime: Runtime, tmp_path: Path
) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store, extra={"second": caps.BlockingCapability(SECOND)})
    module = _module(tmp_path, requested=[VALID])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    _seal(module, version="1.0.0", requested_capabilities=[VALID, SECOND])
    _record, saved = await lifecycle.install(module.with_suffix(".json"), replace=True)

    assert saved.state is InstallState.INSTALLED
    assert saved.capabilities == (VALID, SECOND)
    assert (await lifecycle.inspect(PLUGIN)).approved == ()


async def test_a_changed_digest_takes_the_full_path(runtime: Runtime, tmp_path: Path) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    # A second build of the same version: the payload is part of the bytes.
    rebuilt = _module(
        tmp_path,
        payload={"image": {"artifact_id": "b", "title": "t", "width": 2, "height": 2}},
    )
    _record, saved = await lifecycle.install(rebuilt.with_suffix(".json"), replace=True)

    assert saved.state is InstallState.INSTALLED
    assert (await lifecycle.inspect(PLUGIN)).approved == ()


async def test_a_copy_in_another_directory_is_a_different_installation(
    runtime: Runtime, tmp_path: Path
) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store)
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    module = _module(first)
    await lifecycle.discover(first)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    copy = second / module.name
    copy.write_bytes(module.read_bytes())
    _seal(copy, version="1.0.0")
    await lifecycle.discover(second)
    _record, saved = await lifecycle.install(copy.with_suffix(".json"))

    assert saved.state is InstallState.INSTALLED
    assert (await lifecycle.inspect(PLUGIN)).approved == ()


# -- B1: a guard defect is a refusal, not an execution ------------------------


async def test_a_guard_defect_is_audited_as_a_refusal() -> None:
    capability = _GatedCapability(SECOND)
    capability.release.set()  # it must never be reached; open it so a bug cannot hang
    permissions = _permissions_with_one_shot()

    async def explode(plugin_id: str, name: str) -> None:
        raise RuntimeError("the guard has a defect")

    registry = _registry(permissions, capability=capability, guard=explode)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN,
        declared=frozenset({SECOND}),
        capability=SECOND,
        payload={},
    )

    assert outcome.decision == "not_approved"
    assert outcome.state == "refused"
    assert outcome.admitted is False
    assert outcome.executed is False
    assert "RuntimeError" in outcome.reason
    assert "the guard has a defect" not in outcome.reason  # message logged, not returned
    assert capability.runs == 0


def _permissions_with_one_shot(capability: str = SECOND):
    """A real PermissionManager holding one confirmation for (plugin, capability)."""
    from deskweave_runtime.permissions.manager import PermissionManager

    permissions = PermissionManager()
    permissions.allow_once(principal_for(PLUGIN), capability)
    return permissions


# -- B2: every reservation reaches exactly one disposition --------------------


async def test_a_guard_refusal_returns_the_one_shot() -> None:
    capability = _GatedCapability(SECOND)
    capability.release.set()
    permissions = _permissions_with_one_shot()

    async def refuse(plugin_id: str, name: str) -> None:
        raise plugin_error(PluginNotApprovedError, "not approved right now", plugin_id=plugin_id)

    registry = _registry(permissions, capability=capability, guard=refuse)
    outcome = await registry.dispatch(
        plugin_id=PLUGIN,
        declared=frozenset({SECOND}),
        capability=SECOND,
        payload={},
    )

    assert outcome.decision == "not_approved"
    assert permissions.reserved_one_shots == frozenset()
    assert permissions.outstanding_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})
    assert capability.runs == 0


async def test_a_guard_defect_also_returns_the_one_shot() -> None:
    capability = _GatedCapability(SECOND)
    capability.release.set()
    permissions = _permissions_with_one_shot()

    async def explode(plugin_id: str, name: str) -> None:
        raise RuntimeError("defect")

    registry = _registry(permissions, capability=capability, guard=explode)
    await registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )

    assert permissions.reserved_one_shots == frozenset()
    assert permissions.outstanding_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})


async def test_a_cost_refusal_returns_the_one_shot() -> None:
    capability = caps.BlockingCapability(SECOND, cost=2)
    permissions = _permissions_with_one_shot()
    registry = _registry(permissions, capability=capability)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN,
        declared=frozenset({SECOND}),
        capability=SECOND,
        payload={},
        scope=ExecutionScope(PLUGIN, max_cost=1),
    )

    assert outcome.decision == "cost"
    assert permissions.outstanding_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})
    assert permissions.reserved_one_shots == frozenset()


async def test_a_full_queue_returns_the_one_shot() -> None:
    gate = _GatedCapability(SECOND)
    executor = HostExecutor(max_concurrent=1, blocking_workers=1, max_waiters=1)
    permissions = _permissions_with_one_shot()
    registry = _registry(permissions, capability=gate, executor=executor)
    other = "other.plugin"

    first = asyncio.create_task(
        registry.dispatch(
            plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
        )
    )
    assert await _wait_until(gate.entered.is_set)

    # One waiter allowed, so the next call is refused for a full queue. The
    # allowance has to exist *before* the call runs its permission check, or the
    # call is denied earlier and never reaches admission at all.
    permissions.allow_once(principal_for(other), SECOND)
    waiter = asyncio.create_task(
        registry.dispatch(
            plugin_id=other, declared=frozenset({SECOND}), capability=SECOND, payload={}
        )
    )
    assert await _wait_until(lambda: executor.queued() == 1)

    # A second allowance, so this call gets as far as the queue and is refused
    # by capacity rather than by the permission layer.
    permissions.allow_once(principal_for(other), SECOND)
    outcome = await registry.dispatch(
        plugin_id=other, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )

    assert outcome.decision == "queue_full"
    assert outcome.executed is False
    # The refused call's reservation came back. The one held by the call that is
    # still legitimately in flight has not been settled yet, and a refusal in
    # another call must not even look at it.
    assert permissions.outstanding_one_shots == frozenset({(principal_for(other), SECOND)})
    assert permissions.reserved_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})

    gate.release.set()
    await first
    await waiter

    # Both dispositions are final now: the call that ran spent its confirmation,
    # the refused one still holds the one it got back.
    assert permissions.reserved_one_shots == frozenset()
    assert permissions.outstanding_one_shots == frozenset({(principal_for(other), SECOND)})


async def test_a_cancelled_invocation_returns_the_one_shot() -> None:
    capability = _GatedCapability(SECOND)
    capability.release.set()
    permissions = _permissions_with_one_shot()
    registry = _registry(permissions, capability=capability)
    scope = ExecutionScope(PLUGIN, max_cost=10)
    scope.aborted = True

    outcome = await registry.dispatch(
        plugin_id=PLUGIN,
        declared=frozenset({SECOND}),
        capability=SECOND,
        payload={},
        scope=scope,
    )

    assert outcome.decision == "cancelled"
    assert outcome.executed is False
    assert permissions.outstanding_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})


async def test_a_failure_after_start_spends_the_one_shot() -> None:
    capability = _ExplodingCapability(SECOND)
    permissions = _permissions_with_one_shot()
    registry = _registry(permissions, capability=capability)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )

    assert outcome.state == "failed"
    assert outcome.executed is True
    assert capability.runs == 1
    assert permissions.outstanding_one_shots == frozenset()
    assert permissions.reserved_one_shots == frozenset()


async def test_a_revocation_between_check_and_settlement_does_not_resurrect_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Revocation prunes the reservation, so returning it is a no-op, not a bypass."""
    store = JsonApprovalStore(tmp_path / "approvals.json")
    capability = _GatedCapability(SECOND)
    capability.release.set()
    lifecycle = _lifecycle(runtime, store=store, extra={"second": capability})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [SECOND])

    runtime.permissions.allow_once(principal_for(PLUGIN), SECOND)
    await lifecycle.revoke(PLUGIN)

    # The revocation cleared both the reservation and the allowance it came from.
    assert runtime.permissions.reserved_one_shots == frozenset()
    assert runtime.permissions.outstanding_one_shots == frozenset()

    verdict = await runtime.permissions.check(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN),
            capability=SECOND,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            resource=PLUGIN,
            reason="after revocation",
        )
    )
    assert verdict.decision.value != "allow"


async def test_a_revocation_prunes_a_reservation_the_call_already_holds(
    runtime: Runtime,
) -> None:
    """The line ``revoke`` exists for: a *consumed* one-shot must not come back.

    ``conclude(started=False)`` hands a confirmation back when the call it
    bought never started. That is correct for a refusal and wrong after a
    revocation — the user said stop, and the next call would spend a "yes" they
    gave before saying it. So ``revoke`` drops the *reservation* as well as the
    allowance.

    The revocation test above revokes before any ``check`` ran, so its
    reservation set is empty and the prune never executes; deleting it leaves the
    suite green. Here the reservation is real: the call is inside the capability
    when the revocation lands, which is the only moment the two can cross.
    """
    capability = _GatedCapability(SECOND)
    permissions = _permissions_with_one_shot()
    registry = _registry(permissions, capability=capability)
    scope = ExecutionScope(PLUGIN, max_cost=8)

    call = asyncio.create_task(
        registry.dispatch(
            plugin_id=PLUGIN,
            declared=frozenset({SECOND}),
            capability=SECOND,
            payload={},
            scope=scope,
        )
    )
    assert await asyncio.to_thread(capability.entered.wait, 5.0), "the capability never started"
    assert permissions.reserved_one_shots == frozenset({(principal_for(PLUGIN), SECOND)})

    await permissions.revoke(principal_for(PLUGIN))

    # Without the prune this still holds the reservation: a confirmation that
    # outlived the revocation it was supposed to die with.
    assert permissions.reserved_one_shots == frozenset()

    capability.release.set()
    outcome = await call

    assert outcome.executed is True, "a call already running is not interrupted"
    assert permissions.outstanding_one_shots == frozenset(), "nothing was resurrected"


# -- C1/A2: one store, one revocation barrier --------------------------------


async def test_two_lifecycles_on_one_store_share_the_revocation_barrier(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = tmp_path / "approvals.json"
    first = _lifecycle(runtime, store=JsonApprovalStore(path))
    second = _lifecycle(runtime, store=JsonApprovalStore(path))
    elsewhere = _lifecycle(runtime, store=JsonApprovalStore(tmp_path / "other.json"))

    assert first.host.executor.revocation_barrier is second.host.executor.revocation_barrier
    assert first.host.executor.revocation_barrier is not elsewhere.host.executor.revocation_barrier


async def test_a_queued_call_in_another_host_never_runs_after_a_revocation(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The 2.6.2 cross-instance race, closed at admission.

    Two lifecycles, one store, two executors. A call is held inside the only slot
    of the second host while another call queues behind it; the *first* host then
    revokes. Before the shared barrier the queued call was admitted when the slot
    freed, because it only ever consulted its own executor's block set.
    """
    path = tmp_path / "approvals.json"
    gate = _GatedCapability(SECOND)
    executor = HostExecutor(max_concurrent=1, blocking_workers=1)

    owner = PluginLifecycle(
        registry=default_registry(runtime.permissions),
        permissions=runtime.permissions,
        store=JsonApprovalStore(path),
    )
    other = PluginLifecycle(
        registry=default_registry(runtime.permissions, executor),
        permissions=runtime.permissions,
        store=JsonApprovalStore(path),
    )
    # Both hosts have to offer the capability: the owner needs it to approve,
    # the other one to dispatch.
    owner.registry.register(gate)
    other.registry.register(gate)

    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await owner.discover(tmp_path)
    await owner.install(module.with_suffix(".json"))
    await owner.approve(PLUGIN, [SECOND])

    running = asyncio.create_task(
        other.registry.dispatch(
            plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
        )
    )
    assert await _wait_until(gate.entered.is_set)
    queued = asyncio.create_task(
        other.registry.dispatch(
            plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
        )
    )
    assert await _wait_until(lambda: executor.queued_for(PLUGIN) == 1)

    report = await owner.revoke(PLUGIN)
    assert report.stopped is False
    # The report counts *this* executor's queue, and the queued call is in the
    # other host's. Saying zero here is the honest answer, not an omission: the
    # other host's wait is refused at admission by the shared barrier, which is
    # what the assertions below measure.
    assert report.queued_stopped == 0
    assert report.running == 0

    gate.release.set()
    first_outcome = await running
    queued_outcome = await queued

    assert first_outcome.executed is True  # already running is not preempted
    assert queued_outcome.executed is False
    assert queued_outcome.decision == "revoked"
    assert gate.runs == 1  # only the call that had already started


# -- D1: expired grants are history, not authority ---------------------------


class _ExpiredGrantRepo:
    """A grant repository whose single row expired in 2000."""

    def __init__(self, adapter_id: str, capability: str) -> None:
        self.row = {
            "grant_id": "grant_expired",
            "adapter_id": adapter_id,
            "capability": capability,
            "expires_at": "2000-01-01T00:00:00+00:00",
        }

    async def find(self, adapter_id: str, capability: str):
        if (adapter_id, capability) == (self.row["adapter_id"], self.row["capability"]):
            return dict(self.row)
        return None

    async def list(self):
        return [dict(self.row)]


async def test_an_expired_grant_is_refused_but_kept_as_history() -> None:
    from deskweave_runtime.permissions.manager import PermissionManager

    permissions = PermissionManager(
        grant_repository=_ExpiredGrantRepo(principal_for(PLUGIN), SECOND)
    )
    request = PermissionRequest(
        adapter_id=principal_for(PLUGIN),
        capability=SECOND,
        scope=PermissionScope.PLUGIN_HOST_CALL,
        risk=RiskLevel.LOW,
        requires_confirmation=False,
        resource=PLUGIN,
        reason="expired",
    )

    verdict = await permissions.check(request)

    assert verdict.decision.value != "allow"
    assert await permissions.list_effective_grants() == []
    assert len(await permissions.list_grants()) == 1  # the audit row survives


async def test_approving_after_an_expired_grant_creates_a_live_one(
    runtime: Runtime, tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    lifecycle = _lifecycle(runtime, extra={"second": caps.BlockingCapability(SECOND)})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))

    # A grant that exists in the table but authorises nothing.
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN),
            capability=SECOND,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            resource=PLUGIN,
            reason="stale grant",
        ),
        granted_by="fixture",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    before = await lifecycle.inspect(PLUGIN)
    assert before.granted == ()  # excluded from effective grants

    await lifecycle.approve(PLUGIN, [SECOND])

    after = await lifecycle.inspect(PLUGIN)
    assert after.granted == (SECOND,)
    assert after.approval_in_force is True

    plugin = await lifecycle.activate(module)
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is True


# -- D2: a store failure is a structured result, not a bare OSError ----------


async def test_a_store_failure_while_narrowing_reports_the_partial_state(
    runtime: Runtime, tmp_path: Path
) -> None:
    store = _FailingStore(tmp_path / "approvals.json")
    second = caps.BlockingCapability(SECOND)
    lifecycle = _lifecycle(runtime, store=store, extra={"second": second})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID, SECOND])

    store.fail_saves = True
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve(PLUGIN, [VALID])

    details = caught.value.details
    assert details["approval_saved"] is False
    assert details["revoked_stale"] == [SECOND]
    assert caught.value.__cause__ is not None  # the OSError is preserved

    # Not rolled back, and reported as such: the dropped grant is already gone,
    # the record still holds the previous decision, and that direction is safe.
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.approved == (VALID, SECOND)
    assert inspected.granted == (VALID,)

    # The store is healthy again; what matters is the state the failure left
    # behind, not a second failure layered on top of it.
    store.fail_saves = False
    plugin = await lifecycle.activate(module)
    result = await lifecycle.host.invoke(plugin, {"images": []})
    assert result.ok is False
    assert result.error.code == "plugin_capability_denied"
    assert second.runs == 0


async def test_a_store_failure_while_expanding_keeps_every_grant_in_place(
    runtime: Runtime, tmp_path: Path
) -> None:
    store = _FailingStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(runtime, store=store, extra={"second": caps.BlockingCapability(SECOND)})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    store.fail_saves = True
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve(PLUGIN, [VALID, SECOND])

    assert caught.value.details["approval_saved"] is False
    assert caught.value.details["revoked_stale"] == []
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.approved == (VALID,)
    assert inspected.granted == (VALID,)


# -- post-2.6.3 review: barrier identity, stale lifts, and the ABI authority ---


async def test_two_lifecycles_over_one_memory_store_share_the_barrier(
    runtime: Runtime,
) -> None:
    """Sharing the *store object* has to share the block, path or no path.

    2.6.3 shipped "an in-memory store is instance-local by definition; there is
    nothing another instance could share" — and that was wrong: two lifecycles
    handed the same `MemoryApprovalStore` share one set of records, so they can
    also share a revocation. Measured before the fix: two barriers.
    """
    shared = MemoryApprovalStore()
    first = PluginLifecycle(
        registry=default_registry(runtime.permissions),
        permissions=runtime.permissions,
        store=shared,
    )
    second = PluginLifecycle(
        registry=default_registry(runtime.permissions),
        permissions=runtime.permissions,
        store=shared,
    )
    elsewhere = PluginLifecycle(
        registry=default_registry(runtime.permissions),
        permissions=runtime.permissions,
        store=MemoryApprovalStore(),
    )

    assert first.host.executor.revocation_barrier is second.host.executor.revocation_barrier
    assert first.host.executor.revocation_barrier is not elsewhere.host.executor.revocation_barrier


async def test_a_stale_guard_read_cannot_lift_a_revocation_that_landed_after_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The guard's "approved" is only as fresh as the record it read.

    Before the generation check, this exact sequence ran the call: the guard read
    the pre-revocation record, the other host committed a revocation, and the
    guard then lifted the shared block on its way out — reopening the window the
    block exists to close. The fix refuses the one call whose read is stale.
    """
    path = tmp_path / "approvals.json"
    capability = _GatedCapability(SECOND)
    capability.release.set()  # it must never be reached, so it cannot hang

    owner = PluginLifecycle(
        registry=default_registry(runtime.permissions),
        permissions=runtime.permissions,
        store=JsonApprovalStore(path),
    )
    gated = _GatedReadStore(path)
    other = PluginLifecycle(
        registry=default_registry(runtime.permissions, HostExecutor()),
        permissions=runtime.permissions,
        store=gated,
    )
    owner.registry.register(capability)
    other.registry.register(capability)

    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await owner.discover(tmp_path)
    await owner.install(module.with_suffix(".json"))
    await owner.approve(PLUGIN, [SECOND])

    gated.gate = True
    call = asyncio.create_task(
        other.registry.dispatch(
            plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
        )
    )
    assert await _wait_until(gated.entered.is_set)

    report = await owner.revoke(PLUGIN)
    assert report.stopped is False
    assert other.host.executor.is_revoked(PLUGIN) is True  # the block reached this host

    gated.gate = False
    gated.release.set()
    outcome = await call

    assert outcome.executed is False
    assert outcome.decision != "allow"
    assert capability.runs == 0
    # And the stale approval did not un-block the plugin on its way out.
    assert other.host.executor.is_revoked(PLUGIN) is True


async def test_the_manifest_is_the_only_authority_for_the_abi_version(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Three copies exist; one is the source, two are snapshots. Pin each role.

    * the **manifest on disk** decides compatibility — `activate` refuses when it
      disagrees with this host's `ABI_VERSION`;
    * the **lifecycle record** is the snapshot an approval is bound to, and it
      does not silently follow the file;
    * the **pin** is not consulted by any decision.

    Editing the file while a build is loaded therefore changes nothing about that
    loaded build: same bytes, same approval, so the call is still covered. What it
    must not do is re-authorise anything — and it does not: the next `install()`
    takes the full path, drops the approval, and the next call is refused.
    """
    store = JsonApprovalStore(tmp_path / "approvals.json")
    capability = caps.BlockingCapability(SECOND)
    lifecycle = _lifecycle(runtime, store=store, extra={"second": capability})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    _seal(
        module,
        version="1.0.0",
        requested_capabilities=[SECOND],
        interface_version=ABI_VERSION,
    )
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [SECOND])
    await lifecycle.activate(module)

    # Only the manifest moves; the module bytes stay identical.
    _seal(
        module,
        version="1.0.0",
        requested_capabilities=[SECOND],
        interface_version=ABI_VERSION + 1,
    )
    with pytest.raises(PluginAbiMismatchError) as refused_load:
        await lifecycle.activate(module)
    assert f"interface version {ABI_VERSION + 1}" in refused_load.value.message

    # The record answers "what did I install", not "what does the file say now".
    assert (await lifecycle.inspect(PLUGIN)).interface_version == ABI_VERSION

    # The loaded build is unchanged and still approved, so dispatching to it is
    # still authorised. This is not a re-authorisation: no new build started.
    allowed = await lifecycle.registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )
    assert allowed.decision == "allow"
    assert capability.runs == 1

    _record, saved = await lifecycle.install(module.with_suffix(".json"))
    assert saved.state is InstallState.INSTALLED
    assert saved.interface_version == ABI_VERSION + 1
    assert (await lifecycle.inspect(PLUGIN)).approved == ()

    afterwards = await lifecycle.registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )
    assert afterwards.decision != "allow"
    assert afterwards.executed is False
    assert capability.runs == 1


async def test_a_failed_approval_is_refused_at_every_execution_entry(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A partial approval must not leave a callable capability behind.

    Two entries exist: the module path (`host.invoke`, which reaches the host
    through the bridge) and a direct `registry.dispatch`, which is what an
    embedding application would call. Both are checked here against the same
    failure, plus a control call on a capability that was never dropped — a
    blanket refusal would pass the first two assertions for the wrong reason.
    """
    store = _FailingStore(tmp_path / "approvals.json")
    second = caps.BlockingCapability(SECOND)
    lifecycle = _lifecycle(runtime, store=store, extra={"second": second})
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID, SECOND])

    store.fail_saves = True
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve(PLUGIN, [VALID])
    assert caught.value.details["approval_saved"] is False
    store.fail_saves = False

    # Entry 1 — through the loaded module.
    plugin = await lifecycle.activate(module)
    through_module = await lifecycle.host.invoke(plugin, {"images": []})
    assert through_module.ok is False
    assert through_module.error.code == "plugin_capability_denied"
    assert second.runs == 0

    # Entry 2 — a direct host call.
    direct = await lifecycle.registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )
    assert direct.executed is False
    assert direct.decision != "allow"
    assert second.runs == 0

    # Control: the capability the failure did not touch still runs.
    kept = await lifecycle.registry.dispatch(
        plugin_id=PLUGIN,
        declared=frozenset({VALID}),
        capability=VALID,
        payload=_valid_payload(),
    )
    assert kept.decision == "allow"
    assert kept.executed is True


async def test_a_failed_revocation_leaves_a_working_plugin(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The mirror of the previous test: a revocation that was not recorded did not happen.

    Both directions have to be true, or "fail closed" would quietly turn into
    "fail in whichever direction the code happens to take".
    """
    store = _FailingStore(tmp_path / "approvals.json")
    second = caps.BlockingCapability(SECOND)
    lifecycle = _lifecycle(runtime, store=store, extra={"second": second})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [SECOND])

    store.fail_saves = True
    with pytest.raises(PluginError) as caught:
        await lifecycle.revoke(PLUGIN)
    assert caught.value.details["revocation_persisted"] is False
    store.fail_saves = False

    outcome = await lifecycle.registry.dispatch(
        plugin_id=PLUGIN, declared=frozenset({SECOND}), capability=SECOND, payload={}
    )
    assert outcome.decision == "allow"
    assert second.runs == 1


def test_a_store_path_that_cannot_be_resolved_refuses_to_construct(runtime: Runtime) -> None:
    """No canonical path means no way to know which block to share.

    Running with a private barrier would be a guarantee that quietly is not one,
    so construction fails instead — with the package's own error, not a bare
    `ValueError` from `Path.resolve`.

    Phase 2.6.4 F3 moved the first refusal one step earlier, into the store: the
    path is fixed at construction, so an unresolvable one never becomes a store
    that a lifecycle could then be built on. Both layers are asserted here,
    because the lifecycle still has to refuse a store that *reports* such a path.
    """
    with pytest.raises(PluginLifecycleError) as caught:
        JsonApprovalStore(Path("bad\x00name"))
    assert "cannot be resolved" in caught.value.message

    class _Unresolvable:
        path = Path("bad\x00name")

        async def load_all(self):  # pragma: no cover - refused before use
            return {}

        async def save(self, record):  # pragma: no cover - refused before use
            raise AssertionError("not reached")

    with pytest.raises(PluginLifecycleError) as caught:
        PluginLifecycle(
            registry=default_registry(runtime.permissions),
            permissions=runtime.permissions,
            store=_Unresolvable(),  # type: ignore[arg-type]
        )
    assert "cannot be resolved" in caught.value.message
