"""Phase 2.6.6 P1: an uninstall that fails must not half-happen.

The old order was grants → pin → record, so a failed record write left the pin
deleted and the record still saying ``approved`` (Phase 2.6.5 audit, finding B).
The new order is record → grants → pin: the decision is the first thing written,
which makes "failed" mean "nothing was removed", and a pin that survives a later
step is inert because activation needs an ``approved`` record.

Every test asserts the state on disk, in memory and at the guard, not just the
exception: "it raised" is compatible with every one of the bugs this fixes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import (
    PluginLifecycleError,
    PluginNotApprovedError,
)
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import INSTALL_RECORD_NAME, sha256_file
from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
PLUGIN = "uninstalled"


class CountingPermissions:
    """Authority that can be told to fail its revoke, and counts grants."""

    def __init__(self) -> None:
        self.grant_calls = 0
        self.revoke_calls = 0
        self.fail_revoke: Exception | None = None

    async def list_effective_grants(self) -> list[dict[str, Any]]:
        return []

    async def grant(self, request: Any, **kwargs: Any) -> str:
        self.grant_calls += 1
        return "granted"

    async def revoke(self, principal: str, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        self.revoke_calls += 1
        if self.fail_revoke is not None:
            raise self.fail_revoke
        return ("uninstalled.grant",)


class FailingStore(JsonApprovalStore):
    """Real store, with the next save failed *before* the bytes are written."""

    fail_next: bool = False
    error: Exception = OSError("test: simulated approval-store failure")

    async def save(self, record: Any) -> None:
        if self.fail_next:
            self.fail_next = False
            raise self.error
        await super().save(record)


def _manifest(tmp_path: Path) -> Path:
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
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


def _lifecycle(permissions: Any, store: Any) -> PluginLifecycle:
    return PluginLifecycle(
        registry=default_registry(None, None),
        permissions=permissions,
        store=store,
    )


def _state(store: Path) -> str:
    return json.loads(store.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"]


@pytest.fixture
def setup(tmp_path: Path) -> Any:
    """An installed, approved plugin, plus the pieces a test can break."""
    created: list[PluginLifecycle] = []

    async def build(*, store: Any = None, permissions: Any = None) -> dict[str, Any]:
        store = store if store is not None else FailingStore(tmp_path / "approvals.json")
        permissions = permissions if permissions is not None else CountingPermissions()
        lifecycle = _lifecycle(permissions, store)
        created.append(lifecycle)
        manifest = _manifest(tmp_path)
        await lifecycle.install(manifest)
        await lifecycle.approve(PLUGIN, [VALID])
        return {
            "lifecycle": lifecycle,
            "store": store,
            "permissions": permissions,
            "manifest": manifest,
            "store_path": tmp_path / "approvals.json",
            "pin": manifest.parent / INSTALL_RECORD_NAME,
        }

    yield build
    for lifecycle in created:
        lifecycle.close()


async def test_a_failed_record_write_removes_nothing(setup: Any) -> None:
    """The decision is written first, so failing it must leave the plugin intact."""
    parts = await setup()
    lifecycle, store, pin = parts["lifecycle"], parts["store"], parts["pin"]
    assert pin.is_file()

    store.fail_next = True
    with pytest.raises(PluginLifecycleError) as caught:
        await lifecycle.uninstall(PLUGIN)

    assert caught.value.details["uninstall_persisted"] is False
    assert isinstance(caught.value.__cause__, OSError), "the original failure must be chained"
    assert _state(parts["store_path"]) == "approved"
    assert pin.is_file(), "the pin must not be deleted before the record is written"
    assert parts["permissions"].revoke_calls == 0, "a failed uninstall withdraws no grant"

    # Nothing was half-removed, so the plugin is still exactly as approved.
    await lifecycle._dispatch_guard(PLUGIN, VALID)
    plugin = await lifecycle.activate(parts["manifest"].with_suffix(".wasm"))
    assert plugin.manifest.plugin_id == PLUGIN


async def test_a_failed_grant_withdrawal_is_reported_not_swallowed(setup: Any) -> None:
    """The record is written first, so this failure leaves grants but no authority."""
    parts = await setup()
    lifecycle, pin = parts["lifecycle"], parts["pin"]
    parts["permissions"].fail_revoke = RuntimeError("test: grants store is down")

    with pytest.raises(PluginLifecycleError) as caught:
        await lifecycle.uninstall(PLUGIN)

    assert caught.value.details["uninstall_persisted"] is True
    assert caught.value.details["grants_withdrawn"] is False
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert _state(parts["store_path"]) == "uninstalled"
    assert pin.is_file(), "the pin is only removed after the grants step"

    # A leftover grant authorises nothing: the record refuses the plugin first.
    with pytest.raises(PluginNotApprovedError):
        await lifecycle._dispatch_guard(PLUGIN, VALID)
    with pytest.raises(PluginLifecycleError):
        await lifecycle.activate(parts["manifest"].with_suffix(".wasm"))


async def test_a_pin_that_cannot_be_removed_is_reported_but_does_not_fail_the_uninstall(
    setup: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The record already says uninstalled; a stuck pin is inert, not silent."""
    parts = await setup()
    lifecycle, pin = parts["lifecycle"], parts["pin"]
    pin.unlink()
    pin.mkdir()  # a directory where the file was: unlink raises OSError

    with caplog.at_level("WARNING"):
        record = await lifecycle.uninstall(PLUGIN)

    assert record.state.value == "uninstalled"
    assert _state(parts["store_path"]) == "uninstalled"
    assert pin.is_dir(), "the failed removal is left visible rather than pretending"
    assert any("install record" in message for message in caplog.messages)


async def test_a_retry_after_a_failed_uninstall_does_not_regrant_anything(setup: Any) -> None:
    """Retrying finishes the removal; it never turns a revocation back into a grant."""
    parts = await setup()
    lifecycle, store = parts["lifecycle"], parts["store"]
    grants_before = parts["permissions"].grant_calls

    store.fail_next = True
    with pytest.raises(PluginLifecycleError):
        await lifecycle.uninstall(PLUGIN)

    record = await lifecycle.uninstall(PLUGIN)
    assert record.state.value == "uninstalled"
    assert _state(parts["store_path"]) == "uninstalled"
    assert parts["permissions"].grant_calls == grants_before, "a retry must not grant"
    assert parts["permissions"].revoke_calls == 1
    assert not parts["pin"].is_file()


@pytest.mark.skipif(
    sys.platform != "win32", reason="read-only targets block replace on Windows only"
)
async def test_a_real_filesystem_write_failure_is_the_same_path(setup: Any) -> None:
    """Same failure, produced by the OS rather than by a test double."""
    parts = await setup()
    lifecycle, store_path, pin = parts["lifecycle"], parts["store_path"], parts["pin"]
    os.chmod(store_path, stat.S_IREAD)
    try:
        with pytest.raises(PluginLifecycleError) as caught:
            await lifecycle.uninstall(PLUGIN)
        assert caught.value.details["uninstall_persisted"] is False
        assert isinstance(caught.value.__cause__, OSError)
    finally:
        os.chmod(store_path, stat.S_IWRITE | stat.S_IREAD)

    assert _state(store_path) == "approved"
    assert pin.is_file()
    assert parts["permissions"].revoke_calls == 0
