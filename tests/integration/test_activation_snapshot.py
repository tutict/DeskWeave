"""Phase 2.6.4 Batch B: loading vs authorising, and one manifest snapshot.

F2 was: `host.load()` judged the approval against the host's *cache*, so a build
revoked by another lifecycle was still instantiated. The fix is not a rename —
`load()` now refuses unless it is handed the snapshot that `activate()` takes
under its lock, so there is no path that answers from a cache.

F7 was: `activate()` read the manifest twice (once to find the record, once
inside `load`), so a swap in between could write a record describing one object
while another ran. The fix is one manifest object for the whole activation, with
the digest taken from the bytes that are instantiated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import (
    PluginIntegrityError,
    PluginNotApprovedError,
    PluginNotFoundError,
)
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    LifecycleEvent,
    PluginLifecycle,
)
from deskweave_plugin_wasm.runtime import WasmPluginHost
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
PLUGIN = "snapshot"


def _seal(module: Path, **overrides: object) -> Path:
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=module.stem, wasm_path=module.name, wasm_sha256=digest, **overrides
    )
    module.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    return module


def _reseal(module: Path, *, version: str = "1.0.0", **overrides: object) -> Path:
    """Rewrite the manifest with the shape the approval in these tests covers."""
    return _seal(
        module,
        version=version,
        requested_capabilities=[VALID],
        input_schema={},
        output_schema={},
        **overrides,
    )


def _module(tmp_path: Path, *, version: str = "1.0.0") -> Path:
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
    )
    return _reseal(module, version=version)


def _lifecycle(
    runtime: Runtime, store: Any = None, *, require_install_record: bool = True
) -> PluginLifecycle:
    return PluginLifecycle(
        registry=default_registry(runtime.permissions, None),
        permissions=runtime.permissions,
        store=store,
        require_install_record=require_install_record,
    )


# -- F2 ---------------------------------------------------------------------


async def test_a_direct_load_is_refused_without_a_fresh_snapshot(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The entry itself refuses — proving it is not just a naming convention."""
    lifecycle = _lifecycle(runtime, JsonApprovalStore(tmp_path / "approvals.json"))
    module = _module(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])

    with pytest.raises(PluginNotApprovedError) as caught:
        lifecycle.host.load(module, module.with_suffix(".json"))
    assert "activate" in caught.value.message
    assert "snapshot" in caught.value.message

    plugin = await lifecycle.activate(module)
    assert plugin.manifest.plugin_id == PLUGIN


async def test_a_stale_cache_cannot_authorise_a_load_after_a_revocation(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The reproduced F2: the cache says approved, the store says revoked."""
    path = tmp_path / "approvals.json"
    journey = _lifecycle(runtime, JsonApprovalStore(path))
    other = _lifecycle(runtime, JsonApprovalStore(path))
    module = _module(tmp_path)
    await journey.install(module.with_suffix(".json"))
    await journey.approve(PLUGIN, [VALID])

    await other.revoke(PLUGIN)
    assert journey._records[PLUGIN].state is InstallState.APPROVED, (
        "the cache is stale by design; that is what made the old load unsafe"
    )

    with pytest.raises(PluginNotApprovedError):
        journey.host.load(module, module.with_suffix(".json"))
    with pytest.raises(PluginNotApprovedError) as caught:
        await journey.activate(module)
    assert caught.value.details["state"] == InstallState.REVOKED.value


async def test_the_activation_guard_judges_the_snapshot_it_is_given(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Directly: an empty snapshot is a refusal even though the cache says approved."""
    lifecycle = _lifecycle(runtime, JsonApprovalStore(tmp_path / "approvals.json"))
    module = _module(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve(PLUGIN, [VALID])
    manifest = fixtures.example_manifest(
        plugin_id=PLUGIN,
        wasm_path=module.name,
        wasm_sha256=sha256_file(module)[0],
        version="1.0.0",
        requested_capabilities=[VALID],
        input_schema={},
        output_schema={},
    )
    from deskweave_plugin_wasm.manifest import WasmPluginManifest

    parsed = WasmPluginManifest.model_validate(manifest)
    digest = sha256_file(module)[0]

    lifecycle._guard(parsed, digest, dict(lifecycle._records))  # the live snapshot passes
    with pytest.raises(PluginNotApprovedError):
        lifecycle._guard(parsed, digest, {})  # the cache is not consulted


async def test_a_host_without_a_guard_still_loads(runtime: Runtime, tmp_path: Path) -> None:
    """A host that does not model authorisation is unchanged: it only checks bytes."""
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    module = _module(tmp_path)
    plugin = host.load(module, module.with_suffix(".json"))
    assert plugin.manifest.plugin_id == PLUGIN


# -- F7 ---------------------------------------------------------------------
#
# Two different questions, deliberately kept apart:
#
# * *Was the file read twice?* — answered with a swap that neither the pin nor
#   the approval can object to, so nothing but a second read could notice it.
# * *Is a swap that matters refused?* — answered with a swap that changes the
#   version, which the pin refuses and, in a lifecycle without a pin, the
#   approval check refuses on its own.


async def test_a_swap_between_the_phases_cannot_change_what_was_verified(
    runtime: Runtime, tmp_path: Path
) -> None:
    """One snapshot: only a second read of the manifest could observe the swap.

    The replacement is *pin-equivalent* on purpose — same version, same module,
    same requested capabilities — so `verify_install` and `check_approval` both
    still accept it. That leaves exactly one way to notice it: reading the file
    again. The activation therefore has to finish against the object it verified,
    and the plugin that comes back must describe that object, not the file.
    """
    lifecycle = _lifecycle(runtime, JsonApprovalStore(tmp_path / "approvals.json"))
    module = _module(tmp_path)
    manifest_path = module.with_suffix(".json")
    await lifecycle.install(manifest_path)
    await lifecycle.approve(PLUGIN, [VALID])

    swapped: dict[str, str] = {}
    original_load = lifecycle.host.load

    def swap_then_load(*args: Any, **kwargs: Any):
        # Runs after `activate` has read the manifest and before `load` returns.
        swapped["before"] = manifest_path.read_text(encoding="utf-8")
        _reseal(module, name="Replaced while activating")
        swapped["after"] = manifest_path.read_text(encoding="utf-8")
        return original_load(*args, **kwargs)

    lifecycle.host.load = swap_then_load  # type: ignore[method-assign]

    plugin = await lifecycle.activate(module)
    assert swapped["before"] != swapped["after"], "the swap did not happen"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["name"] == "Replaced while activating"

    # The object that was verified is the object that ran, and the record
    # describes it — not the file that is on disk now.
    assert plugin.manifest.name == "Reference metadata validator"
    assert plugin.manifest.name != on_disk["name"]
    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.ACTIVE
    assert inspected.last_event is LifecycleEvent.ACTIVATED
    assert inspected.last_detail["digest"] == plugin.digest
    stored = json.loads((tmp_path / "approvals.json").read_text(encoding="utf-8"))
    record = stored["plugins"][PLUGIN]
    assert record["state"] == InstallState.ACTIVE.value
    assert record["wasm_sha256"] == plugin.digest


async def test_a_version_swap_between_the_phases_is_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The half that matters: a swap that changes what was approved fails closed.

    The manifest is replaced inside the activation. `verify_install` re-reads it
    as part of pinning the bytes, sees a version that is not the pinned one, and
    refuses — before the engine, and before anything is recorded as active.
    """
    lifecycle = _lifecycle(runtime, JsonApprovalStore(tmp_path / "approvals.json"))
    module = _module(tmp_path)
    manifest_path = module.with_suffix(".json")
    await lifecycle.install(manifest_path)
    await lifecycle.approve(PLUGIN, [VALID])

    original_load = lifecycle.host.load

    def swap_then_load(*args: Any, **kwargs: Any):
        _reseal(module, version="9.9.9")
        return original_load(*args, **kwargs)

    lifecycle.host.load = swap_then_load  # type: ignore[method-assign]

    with pytest.raises(PluginIntegrityError) as caught:
        await lifecycle.activate(module)
    assert caught.value.details["recorded"] == "1.0.0"
    assert caught.value.details["found"] == "9.9.9"

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.APPROVED, "a refused activation must not look active"
    with pytest.raises(PluginNotFoundError):
        # Refused before the engine: nothing was registered on the host.
        lifecycle.host.get(PLUGIN)


async def test_a_manifest_changed_before_activation_is_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A manifest swapped *before* the call is refused by the pin it no longer matches."""
    lifecycle = _lifecycle(runtime, JsonApprovalStore(tmp_path / "approvals.json"))
    module = _module(tmp_path)
    manifest_path = module.with_suffix(".json")
    await lifecycle.install(manifest_path)
    await lifecycle.approve(PLUGIN, [VALID])

    _reseal(module, version="2.0.0")
    with pytest.raises(PluginIntegrityError) as caught:
        await lifecycle.activate(module)
    assert caught.value.details["recorded"] == "1.0.0"
    assert caught.value.details["found"] == "2.0.0"

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.APPROVED, "a refused activation must not look active"


async def test_the_approval_check_alone_refuses_a_changed_version(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Without a pin, the refusal comes from the approval — the same answer.

    This is the layer F7 is about: the approval is bound to a version, so a
    manifest that no longer names that version cannot be activated even when
    nothing else is watching. The record says the activation was refused.
    """
    lifecycle = _lifecycle(
        runtime, JsonApprovalStore(tmp_path / "approvals.json"), require_install_record=False
    )
    module = _module(tmp_path)
    manifest_path = module.with_suffix(".json")
    await lifecycle.install(manifest_path)
    await lifecycle.approve(PLUGIN, [VALID])

    _reseal(module, version="2.0.0")
    with pytest.raises(PluginNotApprovedError) as caught:
        await lifecycle.activate(module)
    assert "version changed" in caught.value.message
    assert caught.value.details["reason"] == "the plugin version changed since it was approved"

    inspected = await lifecycle.inspect(PLUGIN)
    assert inspected.state is InstallState.APPROVED, "a refused activation must not look active"
    assert inspected.last_event is LifecycleEvent.ACTIVATION_REFUSED
    assert "version changed" in inspected.last_detail["reason"]
