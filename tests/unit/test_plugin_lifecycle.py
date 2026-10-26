"""The install lifecycle as a state machine, with no WASM and no runtime.

These tests are deliberately about *decisions*, not bytes: the approval record,
the transition table, and the store's failure modes. Anything that needs a real
module, a real `PermissionManager` or a real host lives in
``tests/integration/test_plugin_lifecycle_runtime.py`` instead — a state machine
test that needs a wasm engine to fail is a test whose failure message lies about
where the bug is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from deskweave_contracts import PermissionRequest
from deskweave_plugin_wasm.errors import PluginError
from deskweave_plugin_wasm.host import default_registry, principal_for
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    InstallState,
    JsonApprovalStore,
    LifecycleEvent,
    MemoryApprovalStore,
    PluginApproval,
    PluginLifecycle,
    check_approval,
    discover,
)
from deskweave_plugin_wasm.manifest import WasmPluginManifest
from support import wasm_plugins as fixtures

CAPABILITY = "reference.metadata.validate"


class RecorderAuthority:
    """Stands in for ``PermissionManager``: records grants, forgets revocations."""

    def __init__(self) -> None:
        self.granted: dict[str, tuple[str, str]] = {}
        self.revoked: list[tuple[str, list[str]]] = []

    async def grant(self, request: PermissionRequest, *, granted_by: str = "user") -> str:
        grant_id = f"grant-{len(self.granted) + 1}"
        self.granted[request.capability] = (grant_id, request.adapter_id)
        return grant_id

    async def revoke(self, adapter_id: str, capabilities: list[str] | None = None) -> list[str]:
        removed = [
            grant_id
            for capability, (grant_id, owner) in self.granted.items()
            if owner == adapter_id and (capabilities is None or capability in capabilities)
        ]
        for capability, (grant_id, _owner) in list(self.granted.items()):
            if grant_id in removed:
                del self.granted[capability]
        self.revoked.append((adapter_id, list(capabilities or [])))
        return removed

    async def list_grants(self) -> list[dict]:
        return [
            {"grant_id": grant_id, "adapter_id": owner, "capability": capability}
            for capability, (grant_id, owner) in self.granted.items()
        ]


def _manifest(module_path: Path) -> WasmPluginManifest:
    return WasmPluginManifest.model_validate(
        json.loads(module_path.with_suffix(".json").read_text(encoding="utf-8"))
    )


def _plugin(tmp_path: Path, name: str = "lifecycle", **overrides: object) -> Path:
    """Write a module plus a manifest that *seals* it, the way a real install does.

    The manifest carries ``wasm_path`` and ``wasm_sha256`` because a lifecycle host
    requires the install record, and that implies the manifest hash too. Sealing
    here keeps the fixture honest: the file on disk is the file the manifest
    describes, so a test that fails is failing for the reason it names.
    """
    module = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response('{"valid":true,"problems":[]}'),
        name=name,
        manifest_dict=fixtures.example_manifest(plugin_id=name),
    )
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=name, wasm_path=module.name, wasm_sha256=digest, **overrides
    )
    module.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    return module


def _lifecycle(authority: RecorderAuthority, **kwargs: object) -> PluginLifecycle:
    return PluginLifecycle(registry=default_registry(), permissions=authority, **kwargs)  # type: ignore[arg-type]


async def _installed(
    tmp_path: Path, authority: RecorderAuthority, **overrides: object
) -> tuple[PluginLifecycle, Path]:
    module = _plugin(tmp_path, "lifecycle", version="1.0.0", **overrides)
    lifecycle = _lifecycle(authority)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    return lifecycle, module


# -- the three sets are separate ----------------------------------------------


async def test_requesting_a_capability_is_not_approving_it(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, _module = await _installed(tmp_path, authority)

    record = await lifecycle.inspect("lifecycle")
    assert record.requested == (CAPABILITY,)
    assert record.approved == ()
    assert authority.granted == {}
    assert record.state is InstallState.INSTALLED


async def test_installing_does_not_create_grants(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, _module = await _installed(tmp_path, authority)
    # One grant-creating path only: approve().
    assert authority.granted == {}
    assert (await lifecycle.inspect("lifecycle")).granted == ()


async def test_approval_creates_exactly_the_granted_capabilities(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, _module = await _installed(tmp_path, authority)
    await lifecycle.approve("lifecycle", [CAPABILITY])

    record = await lifecycle.inspect("lifecycle")
    assert record.approved == (CAPABILITY,)
    assert record.granted == (CAPABILITY,)
    assert record.state is InstallState.APPROVED


async def test_approval_cannot_invent_a_capability(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, _module = await _installed(tmp_path, authority)
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve("lifecycle", [CAPABILITY, "blender.create_reference_plane"])
    assert caught.value.details["not_requested"] == ["blender.create_reference_plane"]
    assert authority.granted == {}


async def test_approval_cannot_name_a_capability_the_host_does_not_offer(
    tmp_path: Path,
) -> None:
    authority = RecorderAuthority()
    module = _plugin(
        tmp_path,
        "lifecycle",
        requested_capabilities=[CAPABILITY, "ghost.capability"],
    )
    lifecycle = _lifecycle(authority)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve("lifecycle", [CAPABILITY, "ghost.capability"])
    assert caught.value.details["unknown"] == ["ghost.capability"]


async def test_approving_a_subset_grants_only_that_subset(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    module = _plugin(
        tmp_path,
        "lifecycle",
        requested_capabilities=[CAPABILITY, "second.capability"],
    )
    registry = default_registry()
    registry.register(_EchoCapability("second.capability"))
    lifecycle = PluginLifecycle(registry=registry, permissions=authority)
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", [CAPABILITY])

    record = await lifecycle.inspect("lifecycle")
    assert record.requested == ("reference.metadata.validate", "second.capability")
    assert record.approved == (CAPABILITY,)
    assert record.granted == (CAPABILITY,)


class _EchoCapability:
    """A second capability, so 'approve a subset' has a subset to leave out."""

    from deskweave_contracts import PermissionScope, RiskLevel

    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.LOW
    cost = 1
    supports_cancellation = False
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, payload: dict) -> dict:  # pragma: no cover - never executed here
        return {}


# -- approval is bound to one build -------------------------------------------


def _approval(**overrides: object) -> PluginApproval:
    base: dict[str, object] = {
        "plugin_id": "lifecycle",
        "version": "1.0.0",
        "interface_version": 1,
        "wasm_sha256": "a" * 64,
        "capabilities": frozenset({CAPABILITY}),
        "requested": frozenset({CAPABILITY}),
        "approved_by": "tester",
        "approved_at": "2026-10-23T00:00:00+00:00",
    }
    base.update(overrides)
    return PluginApproval(**base)  # type: ignore[arg-type]


def _manifest_of(**overrides: object) -> WasmPluginManifest:
    payload = fixtures.example_manifest(plugin_id="lifecycle", version="1.0.0")
    payload.update(overrides)
    return WasmPluginManifest.model_validate(payload)


def test_matching_build_passes() -> None:
    verdict = check_approval(_approval(), _manifest_of(), "a" * 64)
    assert verdict.ok
    assert verdict.added == ()


def test_different_content_is_refused_even_at_the_same_version() -> None:
    verdict = check_approval(_approval(), _manifest_of(), "b" * 64)
    assert not verdict.ok
    assert "content" in verdict.reason


def test_different_version_is_refused() -> None:
    verdict = check_approval(_approval(), _manifest_of(version="1.0.1"), "a" * 64)
    assert not verdict.ok
    assert "version" in verdict.reason


def test_different_abi_is_refused() -> None:
    verdict = check_approval(_approval(), _manifest_of(interface_version=2), "a" * 64)
    assert not verdict.ok
    assert "ABI" in verdict.reason


def test_a_new_capability_in_the_manifest_is_refused_as_escalation() -> None:
    manifest = _manifest_of(requested_capabilities=[CAPABILITY, "extra.capability"])
    verdict = check_approval(_approval(), manifest, "a" * 64)
    assert not verdict.ok
    assert verdict.added == ("extra.capability",)
    assert "not approved" in verdict.reason


def test_a_dropped_capability_is_recorded_but_allowed() -> None:
    manifest = _manifest_of(requested_capabilities=[])
    verdict = check_approval(_approval(), manifest, "a" * 64)
    assert verdict.ok
    assert verdict.dropped == (CAPABILITY,)


def test_an_approval_that_exceeds_the_request_is_reported_as_unused() -> None:
    verdict = check_approval(
        _approval(capabilities=frozenset({CAPABILITY, "extra"})),
        _manifest_of(),
        "a" * 64,
    )
    assert verdict.ok
    assert verdict.unused == ("extra",)


def test_a_different_plugin_id_never_matches() -> None:
    verdict = check_approval(_approval(plugin_id="other"), _manifest_of(), "a" * 64)
    assert not verdict.ok


# -- the transition table ------------------------------------------------------


async def test_activation_without_approval_is_refused(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, module = await _installed(tmp_path, authority)
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_not_approved"
    # The refusal is recorded rather than swallowed.
    record = await lifecycle.inspect("lifecycle")
    assert record.last_event is LifecycleEvent.ACTIVATION_REFUSED


async def test_activation_without_install_is_refused(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    module = _plugin(tmp_path, "lifecycle")
    lifecycle = _lifecycle(authority)
    await lifecycle.discover(tmp_path)
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_lifecycle"
    assert caught.value.details["state"] == "discovered"


async def test_approving_a_plugin_that_is_already_active_is_refused(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, module = await _installed(tmp_path, authority)
    await lifecycle.approve("lifecycle", [CAPABILITY])
    await lifecycle.activate(module)
    with pytest.raises(PluginError) as caught:
        await lifecycle.approve("lifecycle", [CAPABILITY])
    assert caught.value.code == "plugin_lifecycle"
    assert caught.value.details["state"] == "active"


async def test_state_moves_through_the_documented_path(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, module = await _installed(tmp_path, authority)
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.INSTALLED
    await lifecycle.approve("lifecycle", [CAPABILITY])
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.APPROVED
    await lifecycle.activate(module)
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.ACTIVE
    report = await lifecycle.revoke("lifecycle")
    assert report.revoked_grants
    assert (await lifecycle.inspect("lifecycle")).state is InstallState.REVOKED


async def test_revoked_plugin_cannot_be_activated_without_reapproval(tmp_path: Path) -> None:
    authority = RecorderAuthority()
    lifecycle, module = await _installed(tmp_path, authority)
    await lifecycle.approve("lifecycle", [CAPABILITY])
    await lifecycle.activate(module)
    await lifecycle.revoke("lifecycle")
    with pytest.raises(PluginError) as caught:
        await lifecycle.activate(module)
    assert caught.value.code == "plugin_not_approved"


async def test_unknown_plugin_is_reported_as_not_found_not_as_a_state_error() -> None:
    lifecycle = _lifecycle(RecorderAuthority())
    with pytest.raises(PluginError) as caught:
        await lifecycle.inspect("never-seen")
    assert caught.value.code == "plugin_not_found"


# -- the store ----------------------------------------------------------------


async def test_json_store_round_trips_a_record_and_leaves_no_temp_file(tmp_path: Path) -> None:
    store = JsonApprovalStore(tmp_path / "approvals.json")
    lifecycle = _lifecycle(RecorderAuthority(), store=store)
    module = _plugin(tmp_path, "lifecycle")
    await lifecycle.discover(tmp_path)
    await lifecycle.install(module.with_suffix(".json"))
    await lifecycle.approve("lifecycle", [CAPABILITY])

    reloaded = _lifecycle(RecorderAuthority(), store=JsonApprovalStore(tmp_path / "approvals.json"))
    record = await reloaded.inspect("lifecycle")
    assert record.state is InstallState.APPROVED
    assert record.approved == (CAPABILITY,)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []


async def test_a_corrupt_store_is_loud_rather_than_empty(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    path.write_text('{"store_version": 1, "plugins": {"lifecycle": {"state": ', encoding="utf-8")
    lifecycle = _lifecycle(RecorderAuthority(), store=JsonApprovalStore(path))
    with pytest.raises(PluginError) as caught:
        await lifecycle.inspect("lifecycle")
    assert caught.value.code == "plugin_lifecycle"


async def test_a_store_with_the_wrong_shape_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    lifecycle = _lifecycle(RecorderAuthority(), store=JsonApprovalStore(path))
    with pytest.raises(PluginError) as caught:
        await lifecycle.inspect("lifecycle")
    assert caught.value.code == "plugin_lifecycle"


async def test_memory_store_forgets_on_a_new_instance(tmp_path: Path) -> None:
    lifecycle, _module = await _installed(tmp_path, RecorderAuthority())
    await lifecycle.inspect("lifecycle")  # populated
    fresh = _lifecycle(RecorderAuthority(), store=MemoryApprovalStore())
    with pytest.raises(PluginError):
        await fresh.inspect("lifecycle")


# -- discovery ----------------------------------------------------------------


def test_discover_reads_manifests_without_loading_modules(tmp_path: Path) -> None:
    fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response('{"valid":true,"problems":[]}'),
        name="one",
        manifest_dict=fixtures.example_manifest(plugin_id="one", version="2.0.0"),
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    found = discover(tmp_path)
    assert [(item.plugin_id, item.version) for item in found] == [("one", "2.0.0")]
    assert found[0].module_name == "one.wasm"
    assert found[0].pinned is False


def test_discover_skips_the_install_record_itself(tmp_path: Path) -> None:
    fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response('{"valid":true,"problems":[]}'),
        name="one",
        manifest_dict=fixtures.example_manifest(plugin_id="one"),
    )
    (tmp_path / "deskweave.install.json").write_text("{}", encoding="utf-8")
    assert [item.plugin_id for item in discover(tmp_path)] == ["one"]


async def test_revocation_targets_the_plugin_principal_not_another_plugin(
    tmp_path: Path,
) -> None:
    authority = RecorderAuthority()
    lifecycle, _module = await _installed(tmp_path, authority)
    await lifecycle.approve("lifecycle", [CAPABILITY])
    other = await authority.grant(
        PermissionRequest(
            adapter_id=principal_for("other-plugin"),
            capability=CAPABILITY,
            scope=default_registry().capabilities[CAPABILITY].scope,
            risk=default_registry().capabilities[CAPABILITY].risk,
        )
    )
    await lifecycle.revoke("lifecycle")
    assert principal_for("other-plugin") in {owner for _, owner in authority.granted.values()}
    assert other
