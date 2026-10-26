"""Phase 2.6.6 P0: a withdrawal mark belongs to the operation that placed it.

The 2.6.4 mark was a shared *set* plus an unconditional ``discard``, so a second
withdrawal whose store write failed cleared the first withdrawal's mark in its
``finally``. A guard then read the still-``approved`` record, lifted the block,
and admitted a call while an uninstall was between steps (Phase 2.6.5 audit,
finding A). The fix is an ownership token: ``begin_withdrawal`` returns one,
``end_withdrawal`` needs it, and a token can only end the mark it was issued for.

The interleavings below are driven by an ``asyncio.Event`` that a store write
blocks on, never by a sleep: the uninstall is genuinely in flight — queue
drained, record not yet written — when the guard is asked, and the sequence is
the assertion.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import (
    PluginLifecycleError,
    PluginNotApprovedError,
)
from deskweave_plugin_wasm.execution import RevocationBarrier
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    PluginLifecycle,
)
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
PLUGIN = "owned"


class RecordingPermissions:
    """Minimal authority: enough for install/approve/revoke, and it counts."""

    def __init__(self) -> None:
        self.revoked = 0

    async def list_effective_grants(self) -> list[dict[str, Any]]:
        return []

    async def grant(self, request: Any, **kwargs: Any) -> str:
        return "granted"

    async def revoke(self, principal: str, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        self.revoked += 1
        return ("owned.grant",)


class GatedStore(JsonApprovalStore):
    """A store whose ``save`` can be parked — the uninstall's write window."""

    def __init__(self, path: str | Path, *, gate: bool = False) -> None:
        super().__init__(path)
        self.gate = gate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, record: Any) -> None:
        if self.gate:
            self.entered.set()
            await self.release.wait()
        await super().save(record)


class FailOnceStore(JsonApprovalStore):
    """A store whose next save fails, so the "decision not recorded" path runs."""

    fail_next: bool = False

    async def save(self, record: Any) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("test: simulated store write failure")
        await super().save(record)


def _manifest(tmp_path: Path, *, name: str = PLUGIN) -> Path:
    """Write a module plus a sealed manifest; return the *manifest* path."""

    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=name, manifest_dict={}
    )
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=name,
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


def _lifecycle(permissions: Any, store: Any) -> PluginLifecycle:
    return PluginLifecycle(
        registry=default_registry(None, None),
        permissions=permissions,
        store=store,
    )


@pytest.fixture
def opened() -> Any:
    """Every lifecycle this test built, closed afterwards."""
    made: list[PluginLifecycle] = []

    def build(permissions: Any, store: Any) -> PluginLifecycle:
        lifecycle = _lifecycle(permissions, store)
        made.append(lifecycle)
        return lifecycle

    yield build
    for lifecycle in made:
        lifecycle.close()


async def _approved_plugin(tmp_path: Path, opened: Any) -> tuple[PluginLifecycle, GatedStore]:
    owner_store = GatedStore(tmp_path / "approvals.json")
    owner = opened(RecordingPermissions(), owner_store)
    await owner.install(_manifest(tmp_path))
    await owner.approve(PLUGIN, [VALID])
    return owner, owner_store


def _state(tmp_path: Path) -> str:
    raw = json.loads((tmp_path / "approvals.json").read_text(encoding="utf-8"))
    return raw["plugins"][PLUGIN]["state"]


# -- 1. the interleaving from the audit --------------------------------------


async def test_a_failed_second_withdrawal_cannot_clear_the_first_ones_mark(
    tmp_path: Path, opened: Any
) -> None:
    """A's uninstall runs; B's revoke fails; the guard must still refuse.

    This is the exact sequence the 2.6.5 audit reproduced: with one shared,
    unowned mark, B's ``finally`` cleared A's mark, the generation matched (a
    guard captures it *after* the failure), the record still said ``approved``,
    and the call was admitted while the uninstall was parked.
    """
    owner, owner_store = await _approved_plugin(tmp_path, opened)
    failing_store = FailOnceStore(tmp_path / "approvals.json")
    second = opened(RecordingPermissions(), failing_store)
    guard = opened(RecordingPermissions(), JsonApprovalStore(tmp_path / "approvals.json"))

    owner_store.gate = True
    uninstall = asyncio.create_task(owner.uninstall(PLUGIN))
    await asyncio.wait_for(owner_store.entered.wait(), 10)
    try:
        assert _state(tmp_path) == "approved", "the uninstall must be parked before its record"
        assert guard.host.executor.is_withdrawing(PLUGIN)
        with pytest.raises(PluginNotApprovedError) as early:
            await guard._dispatch_guard(PLUGIN, VALID)
        assert "withdrawn" in early.value.message

        failing_store.fail_next = True
        with pytest.raises(PluginLifecycleError) as caught:
            await second.revoke(PLUGIN)
        assert caught.value.details["revocation_persisted"] is False

        # The failure released *its own* mark and nothing else. This is the
        # regression: before the token, the next two assertions were false and
        # the guard admitted the call.
        assert guard.host.executor.is_withdrawing(PLUGIN), "A's mark must survive B's failure"
        with pytest.raises(PluginNotApprovedError) as refused:
            await guard._dispatch_guard(PLUGIN, VALID)
        assert "withdrawn" in refused.value.message
        assert guard.host.executor.is_revoked(PLUGIN)
        assert _state(tmp_path) == "approved"
    finally:
        owner_store.gate = False
        owner_store.release.set()
    await asyncio.wait_for(uninstall, 10)
    assert _state(tmp_path) == "uninstalled"
    assert not guard.host.executor.is_withdrawing(PLUGIN)


# -- 2. ownership ------------------------------------------------------------


def test_a_token_can_only_end_the_mark_it_was_issued_for() -> None:
    """Barrier level, no guard in front of it: the mark is owned, not shared."""
    barrier = RevocationBarrier()
    first = barrier.begin_withdrawal("p")
    second = barrier.begin_withdrawal("p")

    assert barrier.end_withdrawal(second) is True
    assert barrier.withdrawing("p"), "the first withdrawal still holds its own mark"
    assert barrier.blocks("p")

    assert barrier.end_withdrawal(second) is False, "releasing twice releases nothing"
    assert barrier.withdrawing("p")

    assert barrier.end_withdrawal(first) is True
    assert not barrier.withdrawing("p")


def test_a_token_from_another_barrier_releases_nothing() -> None:
    """Two stores are two barriers; a token is not a portable key."""
    here = RevocationBarrier()
    there = RevocationBarrier()
    foreign = there.begin_withdrawal("p")
    here.begin_withdrawal("p")

    assert here.end_withdrawal(foreign) is False
    assert here.withdrawing("p")
    assert there.withdrawing("p")


def test_an_unrelated_token_cannot_clear_a_mark() -> None:
    """A token for another plugin is exactly as powerless as a foreign one."""
    barrier = RevocationBarrier()
    other = barrier.begin_withdrawal("other")
    barrier.begin_withdrawal("p")

    assert barrier.end_withdrawal(other) is True
    assert barrier.end_withdrawal(other) is False
    assert barrier.withdrawing("p")


# -- 3. cleanup on every exit path -------------------------------------------


async def test_a_failed_revocation_releases_only_its_own_mark(tmp_path: Path, opened: Any) -> None:
    """The owner releases on the exception path; nothing is left marked."""
    owner, _store = await _approved_plugin(tmp_path, opened)
    failing = FailOnceStore(tmp_path / "approvals.json")
    broken = opened(RecordingPermissions(), failing)

    failing.fail_next = True
    with pytest.raises(PluginLifecycleError):
        await broken.revoke(PLUGIN)

    assert not broken.host.executor.is_withdrawing(PLUGIN)
    assert _state(tmp_path) == "approved", "a revocation that failed recorded nothing"
    # A revocation that recorded nothing also withdraws the block it placed, and
    # it does so *after* releasing its own mark — which is exactly the order that
    # used to clear somebody else's.
    assert not broken.host.executor.is_revoked(PLUGIN)
    await owner._dispatch_guard(PLUGIN, VALID)
    assert not owner.host.executor.is_withdrawing(PLUGIN)


async def test_a_successful_uninstall_releases_its_mark(tmp_path: Path, opened: Any) -> None:
    """The normal path releases too — a mark that outlives its operation refuses forever."""
    owner, _store = await _approved_plugin(tmp_path, opened)
    await owner.uninstall(PLUGIN)

    assert not owner.host.executor.is_withdrawing(PLUGIN)
    assert _state(tmp_path) == "uninstalled"


# -- 4. several legal withdrawals at once ------------------------------------


def test_interleaved_withdrawals_hold_the_mark_until_the_last_one_ends() -> None:
    """Two in flight: ending either one alone must not reopen the plugin."""
    barrier = RevocationBarrier()
    first = barrier.begin_withdrawal("p")
    second = barrier.begin_withdrawal("p")
    generation = barrier.generation("p")

    assert barrier.lift_if_unchanged("p", generation) is False
    assert barrier.lift("p") is False

    barrier.end_withdrawal(first)
    assert barrier.withdrawing("p")
    assert barrier.lift_if_unchanged("p", generation) is False, "one withdrawal is still open"
    assert barrier.blocks("p")

    barrier.end_withdrawal(second)
    assert not barrier.withdrawing("p")
    assert barrier.blocks("p"), "ending a mark is not a decision about the plugin"
    assert barrier.lift_if_unchanged("p", generation) is True


# -- 5. ending protection is not granting it ---------------------------------


async def test_ending_the_mark_does_not_authorise_anything(tmp_path: Path, opened: Any) -> None:
    """The guard still asks the record, and the record still says revoked."""
    owner, _store = await _approved_plugin(tmp_path, opened)
    await owner.revoke(PLUGIN)

    assert not owner.host.executor.is_withdrawing(PLUGIN)
    assert _state(tmp_path) == InstallState.REVOKED.value
    with pytest.raises(PluginNotApprovedError) as refused:
        await owner._dispatch_guard(PLUGIN, VALID)
    assert "not approved" in refused.value.message


async def test_a_foreign_withdrawal_still_blocks_a_guarded_call(
    tmp_path: Path, opened: Any
) -> None:
    """A mark placed by another instance refuses this one — ownership is not isolation."""
    owner, _store = await _approved_plugin(tmp_path, opened)
    other_store = GatedStore(tmp_path / "approvals.json")
    other = opened(RecordingPermissions(), other_store)

    assert other.host.executor.revocation_barrier is owner.host.executor.revocation_barrier

    other_store.gate = True
    task = asyncio.create_task(other.uninstall(PLUGIN))
    await asyncio.wait_for(other_store.entered.wait(), 10)
    try:
        # Refused by the *mark*, before the record is read: the uninstall has
        # already drained the queue and the record still says approved, so a
        # guard that only consulted the record would admit the call.
        with pytest.raises(PluginNotApprovedError) as refused:
            await owner._dispatch_guard(PLUGIN, VALID)
        assert "withdrawn" in refused.value.message
        assert owner.host.executor.is_withdrawing(PLUGIN)
        assert _state(tmp_path) == "approved"
    finally:
        other_store.gate = False
        other_store.release.set()
    await asyncio.wait_for(task, 10)
    assert _state(tmp_path) == "uninstalled"
