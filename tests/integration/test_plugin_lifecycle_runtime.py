"""The lifecycle against the real host, the real database and real invocations.

Unit tests settle the state machine. This file settles the thing that actually
matters: whether a plugin that was not approved can still reach a capability, and
whether revoking one actually takes the capability away from a module that is
already loaded. Everything here runs real SQLite grants through the real
``PermissionManager``, real WAT modules through the real host, and real threads
for the in-flight case — a mock would answer the questions in the design, not the
questions a reviewer has.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from deskweave_plugin_wasm.errors import PluginError
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import INSTALL_RECORD_NAME, sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    LifecycleEvent,
    PluginLifecycle,
)
from deskweave_runtime import Runtime
from support import host_capabilities as caps
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
SECOND = "test.second"


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
    body: str = '{"ok":true}',
    requested: list[str] | None = None,
    version: str = "1.0.0",
    name: str = "lifecycle",
) -> Path:
    """A module that calls ``capability`` once and then answers ``body``."""
    request = json.dumps({"capability": capability, "payload": payload or _valid_payload()})
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=name, manifest_dict={}
    )
    return _seal(
        module,
        version=version,
        requested_capabilities=requested if requested is not None else [capability],
        # The example plugin's schemas describe a real metadata record; these tests
        # are about authorisation, so leave payload shape out of the picture and
        # let `test_wasm_plugin_negative` keep owning schema failures.
        input_schema={},
        output_schema={},
    )


def _lifecycle(runtime: Runtime, *, store=None, extra: dict | None = None) -> PluginLifecycle:
    registry = default_registry(runtime.permissions)
    for capability in (extra or {}).values():
        registry.register(capability)
    return PluginLifecycle(registry=registry, permissions=runtime.permissions, store=store)


async def _approved(
    runtime: Runtime,
    tmp_path: Path,
    *,
    module: Path | None = None,
    lifecycle: PluginLifecycle | None = None,
    approve: list[str] | None = None,
    **module_kwargs: object,
) -> tuple[PluginLifecycle, object]:
    lifecycle = lifecycle or _lifecycle(runtime)
    module = module or _module(tmp_path, **module_kwargs)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", approve or [VALID])
    plugin = await lifecycle.activate(module)
    return lifecycle, plugin


# -- requested is not granted -------------------------------------------------


async def test_the_manifest_creates_no_grants(runtime: Runtime, tmp_path: Path) -> None:
    lifecycle = _lifecycle(runtime)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))

    inspected = await lifecycle.inspect("lifecycle")
    assert inspected.requested == (VALID,)
    assert inspected.approved == ()
    assert inspected.granted == ()
    # And the permission layer itself agrees: no grant, so a check would deny.
    assert await runtime.permissions.list_grants() == []


async def test_an_approved_capability_is_the_only_one_that_runs(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Two capabilities requested, one approved: the other is refused at dispatch."""
    lifecycle = _lifecycle(runtime, extra={"second": caps.BlockingCapability(SECOND, delay=0.01)})
    # The module calls the *unapproved* capability and lies about the result.
    module = _module(tmp_path, capability=SECOND, requested=[VALID, SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", [VALID])
    plugin = await lifecycle.activate(module)

    result = await lifecycle.host.invoke(plugin, {"images": [_valid_payload()["image"]]})
    assert result.ok is False
    assert result.error.code == "plugin_capability_denied"
    # Refused before anything ran: the capability never executed.
    assert result.host_calls[0].state == "refused"
    assert result.host_calls[0].executed is False
    assert result.host_calls[0].decision == "require_confirmation"
    # The approved capability is still usable in the same plugin.
    assert (await lifecycle.inspect("lifecycle")).approved == (VALID,)


async def test_the_approved_capability_actually_executes(runtime: Runtime, tmp_path: Path) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    result = await lifecycle.host.invoke(plugin, {"images": [_valid_payload()["image"]]})
    assert result.ok is True, result.error
    assert result.host_calls[0].decision == "allow"
    assert result.host_calls[0].executed is True


# -- revocation ---------------------------------------------------------------


async def test_revocation_denies_the_next_call_from_the_same_loaded_plugin(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    first = await lifecycle.host.invoke(plugin, {"images": [_valid_payload()["image"]]})
    assert first.ok is True

    report = await lifecycle.revoke("lifecycle")
    assert report.revoked_grants
    assert await runtime.permissions.list_grants() == []

    second = await lifecycle.host.invoke(plugin, {"images": [_valid_payload()["image"]]})
    assert second.ok is False
    assert second.error.code == "plugin_capability_denied"
    assert second.host_calls[0].state == "refused"
    assert second.host_calls[0].decision == "require_confirmation"
    # The plugin object is still loaded; what changed is the authorisation.
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.REVOKED


async def test_revocation_reports_running_work_and_does_not_claim_it_stopped(
    runtime: Runtime, tmp_path: Path
) -> None:
    slow = caps.BlockingCapability(SECOND, delay=0.40)
    lifecycle = _lifecycle(runtime, extra={"slow": slow})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", [SECOND])
    plugin = await lifecycle.activate(module)

    task = asyncio.create_task(lifecycle.host.invoke(plugin, {"images": []}))
    await asyncio.sleep(0.10)  # the capability is now inside the blocking pool
    assert slow.running == 1

    report = await lifecycle.revoke("lifecycle")
    assert report.was_active is True
    assert report.still_running >= 1
    assert report.stopped is False
    record = await lifecycle.inspect("lifecycle")
    assert record.last_detail["stopped"] is False
    assert record.last_detail["still_running"] >= 1

    # The in-flight call was authorised before the revocation and finishes; a
    # revocation is not a time machine, and saying it stopped the work would be
    # the exact lie this project removed in Phase 2.4.
    result = await task
    assert result.ok is True


async def test_a_revoked_plugin_can_be_reapproved_and_reactivated(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    await lifecycle.revoke("lifecycle")
    with pytest.raises(PluginError):
        await lifecycle.activate(plugin.path)
    await lifecycle.approve("lifecycle", [VALID])
    again = await lifecycle.activate(plugin.path)
    result = await lifecycle.host.invoke(again, {"images": [_valid_payload()["image"]]})
    assert result.ok is True


# -- upgrades -----------------------------------------------------------------


async def test_a_new_version_is_not_covered_by_the_old_approval(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, _plugin = await _approved(runtime, tmp_path)
    # A different build with a different version, in the same directory.
    module = _module(tmp_path, body='{"ok":false}', version="2.0.0")
    await lifecycle.install(module.with_suffix(".json"), replace=True)

    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_not_approved"
    reason = caught.value.details["reason"]
    assert "content" in reason or "version" in reason
    record = await lifecycle.inspect("lifecycle")
    assert record.last_event is LifecycleEvent.ACTIVATION_REFUSED


async def test_a_new_capability_in_a_new_version_is_escalation(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, _plugin = await _approved(runtime, tmp_path)
    module = _module(
        tmp_path,
        capability=SECOND,
        requested=[VALID, SECOND],
        version="1.1.0",
    )
    await lifecycle.install(module.with_suffix(".json"), replace=True)
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_not_approved"
    assert caught.value.details["added"] == [SECOND]


async def test_reapproving_the_upgrade_makes_it_active(runtime: Runtime, tmp_path: Path) -> None:
    lifecycle, _plugin = await _approved(runtime, tmp_path)
    module = _module(tmp_path, body='{"ok":false}', version="2.0.0")
    await lifecycle.install(module.with_suffix(".json"), replace=True)
    await lifecycle.approve("lifecycle", [VALID])
    plugin = await lifecycle.activate(module)
    assert plugin.manifest.version == "2.0.0"


# -- install failure and strict mode ------------------------------------------


async def test_a_failed_install_leaves_no_record_and_no_state_change(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle = _lifecycle(runtime)
    module = _module(tmp_path)
    await lifecycle.discover(tmp_path)

    manifest_path = module.with_suffix(".json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["wasm_sha256"] = "0" * 64  # a lie about the bytes
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginError) as caught:
        await lifecycle.install(manifest_path)
    assert caught.value.code == "plugin_install"
    assert not (tmp_path / INSTALL_RECORD_NAME).exists()
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.DISCOVERED


async def test_strict_mode_refuses_activation_without_a_pin(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    (tmp_path / INSTALL_RECORD_NAME).unlink()
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(plugin.path)
    assert caught.value.code == "plugin_integrity"


async def test_a_corrupt_pin_is_refused_rather_than_ignored(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    (tmp_path / INSTALL_RECORD_NAME).write_text(
        '{"record_version": 1, "plugin_id": ', encoding="utf-8"
    )
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(plugin.path)
    assert caught.value.code == "plugin_integrity"


# -- uninstall ----------------------------------------------------------------


async def test_uninstall_removes_pin_and_grants_but_not_the_module(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    record = await lifecycle.uninstall("lifecycle")

    assert record.state is InstallState.UNINSTALLED
    assert record.approval is None
    assert not (tmp_path / INSTALL_RECORD_NAME).exists()
    assert await runtime.permissions.list_grants() == []
    # The module is the user's file; this tool does not delete it.
    assert plugin.path.is_file()
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(plugin.path)
    assert caught.value.code in {"plugin_lifecycle", "plugin_integrity"}


async def test_uninstall_refuses_while_an_operation_is_running(
    runtime: Runtime, tmp_path: Path
) -> None:
    slow = caps.BlockingCapability(SECOND, delay=0.40)
    lifecycle = _lifecycle(runtime, extra={"slow": slow})
    module = _module(tmp_path, capability=SECOND, requested=[SECOND])
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", [SECOND])
    plugin = await lifecycle.activate(module)

    task = asyncio.create_task(lifecycle.host.invoke(plugin, {"images": []}))
    await asyncio.sleep(0.10)
    with pytest.raises(PluginError) as caught:
        await lifecycle.uninstall("lifecycle")
    assert caught.value.code == "plugin_lifecycle"
    assert caught.value.details["still_running"] >= 1

    await task
    record = await lifecycle.uninstall("lifecycle")
    assert record.state is InstallState.UNINSTALLED


async def test_after_uninstall_a_reinstall_does_not_restore_the_old_approval(
    runtime: Runtime, tmp_path: Path
) -> None:
    lifecycle, plugin = await _approved(runtime, tmp_path)
    await lifecycle.uninstall("lifecycle")
    await lifecycle.install(plugin.path.with_suffix(".json"))
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.INSTALLED
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(plugin.path)
    assert caught.value.code == "plugin_not_approved"
