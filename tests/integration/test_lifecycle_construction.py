"""Phase 2.6.6 P2: a failed construction must leave nothing behind.

``registry.dispatch_guard`` used to be installed *before* the store lock was
taken and before the host existed, so a refused construction left the caller's
registry pointing at a lifecycle with no ``_host`` — dispatch then raised
``AttributeError`` from inside the guard (Phase 2.6.5 audit, probe B4). The guard
is installed last now, and a construction that fails releases the lock it took.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import (
    PluginNotApprovedError,
    PluginStoreLockedError,
)
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle
from deskweave_plugin_wasm.locking import held_keys
from support.store_lock import Holder


class Boom(RuntimeError):
    """A host that cannot be built."""


def _key(store: Path) -> str:
    return os.path.normcase(str(store.resolve()))


def test_a_refused_lock_leaves_the_registry_untouched(tmp_path: Path) -> None:
    """The failure the audit found: a guard bound to a lifecycle with no host."""
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    registry = default_registry(None, None)
    assert registry.dispatch_guard is None

    with Holder(store, tmp_path / "ready.json") as holder:
        holder.wait_ready()
        with pytest.raises(PluginStoreLockedError):
            PluginLifecycle(
                registry=registry,
                permissions=None,
                store=JsonApprovalStore(store),
            )

    assert registry.dispatch_guard is None, "a refused construction must install no guard"
    assert _key(store) not in held_keys()


def test_a_failed_host_initialisation_releases_the_lock(tmp_path: Path, monkeypatch: Any) -> None:
    """The lock is acquired before the host; a host failure must give it back."""
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    registry = default_registry(None, None)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise Boom("test: the host could not be built")

    monkeypatch.setattr("deskweave_plugin_wasm.lifecycle.WasmPluginHost", explode)

    with pytest.raises(Boom):
        PluginLifecycle(
            registry=registry,
            permissions=None,
            store=JsonApprovalStore(store),
        )

    assert registry.dispatch_guard is None
    assert _key(store) not in held_keys(), "the failed construction must not keep the store"


def test_a_failed_construction_does_not_clear_a_guard_the_caller_set(tmp_path: Path) -> None:
    """What the caller put there before is not ours to remove."""
    store = tmp_path / "approvals.json"
    store.write_text('{"record_version": 1, "plugins": {}}', encoding="utf-8")
    registry = default_registry(None, None)

    async def caller_guard(plugin_id: str, capability: str) -> None:
        return None

    registry.dispatch_guard = caller_guard

    with Holder(store, tmp_path / "ready.json") as holder:
        holder.wait_ready()
        with pytest.raises(PluginStoreLockedError):
            PluginLifecycle(
                registry=registry,
                permissions=None,
                store=JsonApprovalStore(store),
            )

    assert registry.dispatch_guard is caller_guard


async def test_a_successful_construction_installs_its_own_guard(tmp_path: Path) -> None:
    """Last, but not never: the working case still binds and still refuses."""
    store = tmp_path / "approvals.json"
    registry = default_registry(None, None)
    lifecycle = PluginLifecycle(
        registry=registry,
        permissions=None,
        store=JsonApprovalStore(store),
    )
    try:
        assert registry.dispatch_guard == lifecycle._dispatch_guard
        assert not lifecycle.closed
        # And it works: an unknown plugin is refused by the guard, not by an
        # AttributeError from a half-built object.
        with pytest.raises(PluginNotApprovedError) as refused:
            await registry.dispatch_guard("unknown", "reference.metadata.validate")
        assert "no approval record" in refused.value.message
    finally:
        lifecycle.close()
