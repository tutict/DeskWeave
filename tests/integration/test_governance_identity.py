"""Phase 2.6.4 Batch A: store identity, barrier sharing, executor binding.

Each test is written against a failure that was reproduced before the fix, and
every wait is a ``threading.Event`` or a counter — no ``sleep`` decides an
outcome here.

Findings, as numbered in the task:

* **F1** — ``uninstall`` blocked *after* writing nothing and ``revoke`` wrote
  first; the second lifecycle's guard could read the still-approved record and
  lift the block in between. The shared *withdrawal mark* is what closes it.
* **F3** — the store kept the caller's relative path, so the barrier was keyed at
  construction while the file was resolved at use; a ``chdir`` in between split
  one store into two barriers.
* **F4** — a hard link is the same file under two names, so it got two barriers.
  Refused at construction rather than documented as supported.
* **F5** — a pathless store that could not be weak-referenced silently fell back
  to a private barrier, while the path branch failed closed.
* **F6** — one executor adopted two barriers in turn; the second replaced the
  first and unblocked the plugins it held.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from deskweave_contracts import PermissionScope, RiskLevel
from deskweave_plugin_wasm.errors import PluginLifecycleError, PluginNotApprovedError
from deskweave_plugin_wasm.execution import ExecutionScope, HostExecutor
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    LifecycleRecord,
    MemoryApprovalStore,
    PluginLifecycle,
)
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
SECOND = "test.second"
PLUGIN = "identity"


# -- fixtures ----------------------------------------------------------------


def _seal(module: Path, **overrides: object) -> Path:
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=module.stem, wasm_path=module.name, wasm_sha256=digest, **overrides
    )
    module.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    return module


def _module(tmp_path: Path, *, capability: str = VALID, requested: list[str] | None = None) -> Path:
    request = json.dumps(
        {
            "capability": capability,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
    )
    return _seal(
        module,
        version="1.0.0",
        requested_capabilities=requested if requested is not None else [capability],
        input_schema={},
        output_schema={},
    )


def _lifecycle(
    runtime: Runtime,
    *,
    store: Any = None,
    executor: HostExecutor | None = None,
    extra: dict[str, Any] | None = None,
) -> PluginLifecycle:
    registry = default_registry(runtime.permissions, executor)
    for capability in (extra or {}).values():
        registry.register(capability)
    return PluginLifecycle(registry=registry, permissions=runtime.permissions, store=store)


class _HookedStore(JsonApprovalStore):
    """Holds ``save`` open so a test can act inside the write window."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()

    async def save(self, record: LifecycleRecord) -> None:
        if self.hold:
            self.entered.set()
            await asyncio.get_running_loop().run_in_executor(None, self.release.wait, 10.0)
        await super().save(record)


class _Slotless:
    """A pathless store that is neither weak-referenceable nor hashable."""

    __slots__ = ("rows",)

    def __init__(self) -> None:
        self.rows: dict[str, LifecycleRecord] = {}

    async def load_all(self):
        return dict(self.rows)

    async def save(self, record):
        raise AssertionError("not reached")


class _Gated:
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
        self.release.wait(10.0)
        return {"ran": True}


def _scope() -> ExecutionScope:
    return ExecutionScope(PLUGIN, max_cost=10)


async def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


# -- F3: the store's path is fixed at construction ---------------------------


async def test_a_relative_store_path_is_fixed_at_construction(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later ``chdir`` must not move the store — or its barrier.

    Reproduced before the fix: L1 built in ``a/`` and L2 built in ``b/`` with the
    same relative spelling ended up reading one file through two barriers, so a
    revocation through L1 left L2's queued call admissible.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    store = JsonApprovalStore("approvals.json")
    assert store.path.is_absolute()
    assert store.path == (first / "approvals.json").resolve()

    monkeypatch.chdir(second)
    assert store.path == (first / "approvals.json").resolve(), "the store moved with the cwd"
    assert not (second / "approvals.json").exists()

    # the barrier is keyed by that fixed path, not by the spelling or the cwd
    left = _lifecycle(runtime, store=JsonApprovalStore(first / "approvals.json"))
    monkeypatch.chdir(second)
    right = _lifecycle(runtime, store=JsonApprovalStore("approvals.json"))
    # `right` was built in `b/`, so it names a *different* file — different on
    # purpose, and the assertion is that identity follows the path, not the cwd.
    assert left.host.executor.revocation_barrier is not right.host.executor.revocation_barrier
    same = _lifecycle(runtime, store=JsonApprovalStore(str(first / "approvals.json")))
    assert left.host.executor.revocation_barrier is same.host.executor.revocation_barrier


async def test_a_store_reached_through_two_names_is_refused(tmp_path: Path) -> None:
    """F4: a hard link is one file under two names, so it gets two barriers.

    Supporting that needs a portable file identity that survives replacement, so
    the configuration is refused instead of silently degrading. ``resolve()``
    cannot see it — the assertion below is exactly that.
    """
    store = tmp_path / "approvals.json"
    store.write_text('{"plugins": {}}', encoding="utf-8")
    link = tmp_path / "approvals-copy.json"
    try:
        os.link(store, link)
    except OSError as exc:  # pragma: no cover - filesystem without hard links
        pytest.skip(f"hard links are not available here: {exc}")

    assert os.path.samefile(store, link)
    assert store.resolve() != link.resolve(), "resolve() does not identify the file"

    with pytest.raises(PluginLifecycleError) as caught:
        JsonApprovalStore(link)
    assert caught.value.details["links"] == 2
    assert "hard link" in caught.value.message


# -- F1: the withdrawal window ----------------------------------------------


async def test_another_lifecycle_cannot_lift_the_block_an_uninstall_placed(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The reproduced F1: block first, record last, guard in between.

    Before the fix ``uninstall`` placed the block and then wrote the record; a
    second lifecycle's guard read the still-approved record inside that window,
    answered "approved", and lifted the block — measured as
    ``L2 guard during window: ALLOWED``.
    """
    path = tmp_path / "approvals.json"
    path.write_text('{"plugins": {}}', encoding="utf-8")
    hooked = _HookedStore(path)
    owner = _lifecycle(runtime, store=hooked)
    other = _lifecycle(runtime, store=JsonApprovalStore(path))
    module = _module(tmp_path)
    await owner.install(module.with_suffix(".json"))
    await owner.approve(PLUGIN, [VALID])
    assert owner.host.executor.revocation_barrier is other.host.executor.revocation_barrier

    hooked.hold = True
    task = asyncio.create_task(owner.uninstall(PLUGIN))
    assert await asyncio.to_thread(hooked.entered.wait, 5.0), "uninstall never reached the write"

    assert owner.host.executor.is_revoked(PLUGIN), "the withdrawal must block before it writes"
    record = json.loads(path.read_text(encoding="utf-8"))["plugins"][PLUGIN]
    assert record["state"] == InstallState.APPROVED.value, "the record is written last, on purpose"

    with pytest.raises(PluginNotApprovedError) as caught:
        await other._dispatch_guard(PLUGIN, VALID)
    assert "withdrawn" in caught.value.message
    assert other.host.executor.is_revoked(PLUGIN), "the second guard lifted the block"

    hooked.release.set()
    await task
    assert json.loads(path.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"] == "uninstalled"
    assert not other.host.executor.is_withdrawing(PLUGIN)


async def test_a_revoke_refuses_guards_in_another_lifecycle_before_it_writes(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``revoke`` writes first, but the mark covers the write itself."""
    path = tmp_path / "approvals.json"
    path.write_text('{"plugins": {}}', encoding="utf-8")
    hooked = _HookedStore(path)
    owner = _lifecycle(runtime, store=hooked)
    other = _lifecycle(runtime, store=JsonApprovalStore(path))
    module = _module(tmp_path)
    await owner.install(module.with_suffix(".json"))
    await owner.approve(PLUGIN, [VALID])

    hooked.hold = True
    task = asyncio.create_task(owner.revoke(PLUGIN))
    assert await asyncio.to_thread(hooked.entered.wait, 5.0)

    with pytest.raises(PluginNotApprovedError):
        await other._dispatch_guard(PLUGIN, VALID)
    hooked.release.set()
    await task
    assert json.loads(path.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"] == "revoked"

    with pytest.raises(PluginNotApprovedError):
        await other._dispatch_guard(PLUGIN, VALID)


async def test_a_failed_revocation_removes_only_its_own_block(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A refusal to record must not leave the plugin blocked by *this* call.

    The block placed before the write belongs to a decision that was not taken,
    so it is removed when the write fails — and the withdrawal mark with it, or
    every later guard would refuse with "being withdrawn".
    """

    class _Failing(JsonApprovalStore):
        fail = False

        async def save(self, record):
            if self.fail:
                raise OSError("the store is unreachable")
            await super().save(record)

    path = tmp_path / "approvals.json"
    path.write_text('{"plugins": {}}', encoding="utf-8")
    store = _Failing(path)
    lifecycle = _lifecycle(runtime, store=store)
    module = _module(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    store.fail = True
    with pytest.raises(Exception) as caught:
        await lifecycle.revoke(PLUGIN)
    assert caught.value.details["revocation_persisted"] is False  # type: ignore[attr-defined]

    assert not lifecycle.host.executor.is_revoked(PLUGIN)
    assert not lifecycle.host.executor.is_withdrawing(PLUGIN)
    # the approval is still there, so the next call is admitted again
    await lifecycle._dispatch_guard(PLUGIN, VALID)


# -- F1: the barrier's own rule, separately from the guard's early refusal ---


def test_both_lift_paths_refuse_while_a_withdrawal_is_in_progress() -> None:
    """The second line of defence, asserted without the guard in front of it.

    ``_dispatch_guard`` refuses early when it sees the mark, so a test that only
    goes through the guard cannot tell whether the *barrier* would also have
    refused the lift. This one asks the barrier directly: if the generation check
    ignored the mark, an ``approve`` running concurrently with an uninstall could
    unfreeze the plugin mid-withdrawal.
    """
    from deskweave_plugin_wasm.execution import RevocationBarrier

    barrier = RevocationBarrier()
    withdrawal = barrier.begin_withdrawal("p")
    generation = barrier.generation("p")
    assert barrier.blocks("p")
    assert barrier.withdrawing("p")

    assert barrier.lift_if_unchanged("p", generation) is False, "the mark must veto the lift"
    assert barrier.lift("p") is False, "an unconditional lift must not win either"
    assert barrier.blocks("p")

    assert barrier.end_withdrawal(withdrawal) is True
    assert not barrier.withdrawing("p")
    assert barrier.lift_if_unchanged("p", generation) is True
    assert not barrier.blocks("p")


# -- F5: a store identity that cannot be shared is a refusal ----------------


async def test_a_pathless_store_that_cannot_be_weak_referenced_is_refused(
    runtime: Runtime,
) -> None:
    """F5: the pathless branch used to fall back to a private barrier."""
    store = _Slotless()
    with pytest.raises(PluginLifecycleError) as caught:
        _lifecycle(runtime, store=store)
    assert caught.value.details["store"] == "_Slotless"
    assert "shared identity" in caught.value.message


async def test_two_distinct_memory_stores_do_not_share_a_barrier(runtime: Runtime) -> None:
    """The object is the identity, so two objects are two identities — not zero."""
    left = _lifecycle(runtime, store=MemoryApprovalStore())
    right = _lifecycle(runtime, store=MemoryApprovalStore())
    assert left.host.executor.revocation_barrier is not right.host.executor.revocation_barrier


# -- F6: one executor, one store -------------------------------------------


async def test_an_executor_cannot_be_bound_to_two_stores(runtime: Runtime, tmp_path: Path) -> None:
    """F6: the second adoption used to replace the first, silently.

    The refusal is at construction, before either lifecycle can hand out a plugin
    object, so there is no window in which a half-bound executor is usable.
    """
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for path in (first, second):
        path.write_text('{"plugins": {}}', encoding="utf-8")
    executor = HostExecutor(max_concurrent=2, blocking_workers=1)

    owner = _lifecycle(runtime, store=JsonApprovalStore(first), executor=executor)
    with pytest.raises(PluginLifecycleError) as caught:
        _lifecycle(runtime, store=JsonApprovalStore(second), executor=executor)
    assert "already bound" in caught.value.message
    # the first binding is still the one in force
    assert owner.host.executor.revocation_barrier is executor.revocation_barrier


# -- generation state machine over a full cycle ----------------------------


async def test_generations_advance_across_approve_revoke_uninstall_reinstall(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Every withdrawal moves the generation forward, and the state stays legal.

    The generation answers "has anything been withdrawn since I read the record",
    so it must never move backwards across a full cycle — including the
    reinstall, which clears the approval but is not itself a withdrawal.
    """
    path = tmp_path / "approvals.json"
    path.write_text('{"plugins": {}}', encoding="utf-8")
    lifecycle = _lifecycle(runtime, store=JsonApprovalStore(path))
    module = _module(tmp_path)
    executor = lifecycle.host.executor

    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])
    started = executor.revocation_generation(PLUGIN)

    await lifecycle.revoke(PLUGIN)
    after_revoke = executor.revocation_generation(PLUGIN)
    assert after_revoke > started

    await lifecycle.approve(PLUGIN, [VALID])
    assert await lifecycle._dispatch_guard(PLUGIN, VALID) is None
    assert executor.revocation_generation(PLUGIN) >= after_revoke

    await lifecycle.revoke(PLUGIN)
    after_second = executor.revocation_generation(PLUGIN)
    assert after_second > after_revoke

    await lifecycle.uninstall(PLUGIN)
    after_uninstall = executor.revocation_generation(PLUGIN)
    assert after_uninstall > after_second

    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])
    state = json.loads(path.read_text(encoding="utf-8"))["plugins"][PLUGIN]
    assert state["state"] == InstallState.APPROVED.value
    assert executor.revocation_generation(PLUGIN) == after_uninstall, "install is not a withdrawal"


async def test_a_queued_call_in_a_second_lifecycle_never_starts_after_an_uninstall(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The queue is drained before any of this matters, but the second host's is not.

    The call is submitted straight to the executor: that is the position a call
    occupies once the guard has authorised it, which is exactly what the shared
    barrier has to protect.
    """
    path = tmp_path / "approvals.json"
    path.write_text('{"plugins": {}}', encoding="utf-8")
    gate = _Gated()
    executor = HostExecutor(max_concurrent=1, blocking_workers=1)
    owner = _lifecycle(runtime, store=JsonApprovalStore(path))
    other = _lifecycle(
        runtime, store=JsonApprovalStore(path), executor=executor, extra={"gate": gate}
    )
    module = _module(tmp_path)
    await owner.install(module.with_suffix(".json"))
    await owner.approve(PLUGIN, [VALID])

    first = asyncio.create_task(
        executor.run(
            plugin_id=PLUGIN,
            capability=SECOND,
            handler=gate,
            payload={},
            scope=_scope(),
            timeout_s=30,
        )
    )
    assert await asyncio.to_thread(gate.entered.wait, 5.0)
    queued = asyncio.create_task(
        executor.run(
            plugin_id=PLUGIN,
            capability=SECOND,
            handler=gate,
            payload={},
            scope=_scope(),
            timeout_s=30,
        )
    )
    assert await _wait_until(lambda: executor.queued_for(PLUGIN) == 1)

    await owner.uninstall(PLUGIN)
    gate.release.set()
    await asyncio.gather(first, queued, return_exceptions=True)
    assert gate.runs == 1, "the queued call was admitted after the uninstall"
    assert other.host.executor.is_revoked(PLUGIN)
