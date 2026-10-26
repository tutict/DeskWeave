"""The authorisation boundary itself: what makes an ALLOW sufficient for a plugin.

`PermissionManager` answers the same question for adapters and for plugins, but the
answer is not interchangeable. For an adapter, "policy says read-only is fine" is a
complete answer. For a sandboxed plugin it is not: the plugin *declares* what it
wants, so a policy default that allows a declaration would let the plugin authorise
itself. These tests pin the difference down at the dispatch layer, where the
decision to execute actually happens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from deskweave_contracts import (
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    PermissionScope,
    RiskLevel,
)
from deskweave_plugin_wasm.host import HostCapabilityRegistry, default_registry, principal_for
from deskweave_plugin_wasm.runtime import WasmPluginHost
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

READ_ONLY_CAP = "test.read_only"
VALID = "reference.metadata.validate"


class _ReadOnlyCapability:
    """A capability whose *policy* class is read-only — the trap this file exists for."""

    name = READ_ONLY_CAP
    scope = PermissionScope.PLUGIN_HOST_CALL
    risk = RiskLevel.READ_ONLY
    parameters_schema: ClassVar[dict] = {"type": "object"}
    cost = 1
    supports_cancellation = False

    def __init__(self) -> None:
        self.runs = 0

    def run(self, payload: dict) -> dict:
        self.runs += 1
        return {"ran": True}


class _StubChecker:
    """A checker that says yes and does not say why — the third-party case."""

    async def check(self, request):
        return PermissionResult(decision=PermissionDecision.ALLOW, reason="stub says yes")


def fixtures_permission_request(capability: str, risk: RiskLevel, scope: PermissionScope):
    """The request shape ``PermissionManager.grant`` expects, for one plugin."""
    return PermissionRequest(
        adapter_id=principal_for("boundary"),
        capability=capability,
        scope=scope,
        risk=risk,
        resource="boundary",
    )


def _plugin_requesting(tmp_path, capability: str, name: str = "boundary"):
    request = json.dumps({"capability": capability, "payload": {}})
    return fixtures.write_plugin(
        tmp_path,
        fixtures.host_request(request),
        name=name,
        manifest_dict=fixtures.manifest(plugin_id=name, requested_capabilities=[capability]),
    )


def _host(runtime: Runtime, capability=None) -> WasmPluginHost:
    registry = default_registry(runtime.permissions)
    if capability is not None:
        registry.register(capability)
    return WasmPluginHost(registry=registry)


async def test_a_read_only_capability_cannot_authorise_itself(
    runtime: Runtime, tmp_path: Path
) -> None:
    capability = _ReadOnlyCapability()
    host = _host(runtime, capability)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    plugin = host.load(module)

    result = await host.invoke(plugin, {})
    assert result.ok is False
    assert result.error.code == "plugin_capability_denied"
    call = result.host_calls[0]
    assert call.decision == "policy_default"
    assert call.state == "refused"
    assert call.executed is False
    # And nothing ran: the whole point of refusing before execution.
    assert capability.runs == 0


async def test_the_same_read_only_capability_runs_once_it_is_granted(
    runtime: Runtime, tmp_path: Path
) -> None:
    capability = _ReadOnlyCapability()
    host = _host(runtime, capability)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    plugin = host.load(module)

    await runtime.permissions.grant(
        fixtures_permission_request(READ_ONLY_CAP, capability.risk, capability.scope),
        granted_by="test",
    )
    result = await host.invoke(plugin, {})
    assert result.ok is True, result.error
    assert capability.runs == 1
    assert result.host_calls[0].decision == "allow"


async def test_a_one_shot_allowance_is_used_once(runtime: Runtime, tmp_path: Path) -> None:
    capability = _ReadOnlyCapability()
    host = _host(runtime, capability)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    plugin = host.load(module)

    runtime.permissions.allow_once(principal_for("boundary"), READ_ONLY_CAP)
    first = await host.invoke(plugin, {})
    second = await host.invoke(plugin, {})
    assert first.ok is True
    assert second.ok is False
    assert capability.runs == 1


async def test_revocation_discards_a_pending_one_shot(runtime: Runtime, tmp_path: Path) -> None:
    """The in-memory half of the state: a one-shot survives a database sweep."""
    capability = _ReadOnlyCapability()
    host = _host(runtime, capability)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    plugin = host.load(module)

    runtime.permissions.allow_once(principal_for("boundary"), READ_ONLY_CAP)
    removed = await runtime.permissions.revoke(principal_for("boundary"))
    assert removed == []  # nothing was in the database, only in memory
    result = await host.invoke(plugin, {})
    assert result.ok is False
    assert capability.runs == 0


async def test_an_allow_without_provenance_is_not_trusted(runtime: Runtime, tmp_path: Path) -> None:
    """A checker that cannot say *why* cannot authorise a plugin call."""
    registry = HostCapabilityRegistry(
        permission_checker=_StubChecker(),
        capabilities={READ_ONLY_CAP: _ReadOnlyCapability()},
    )
    host = WasmPluginHost(registry=registry)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    plugin = host.load(module)

    result = await host.invoke(plugin, {})
    assert result.ok is False
    assert result.host_calls[0].decision == "policy_default"
    assert "unnamed" in result.host_calls[0].reason


async def test_an_undeclared_capability_is_refused_before_policy(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Declaration is checked before authorisation, so the policy never runs.

    The module calls a capability that exists and that a grant would allow, but
    the manifest never declared it. The refusal must name *that*, not "denied",
    because the two call for different fixes.
    """
    capability = _ReadOnlyCapability()
    host = _host(runtime, capability)
    module = _plugin_requesting(tmp_path, READ_ONLY_CAP)
    manifest_path = module.with_suffix(".json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["requested_capabilities"] = [VALID]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    plugin = host.load(module)

    await runtime.permissions.grant(
        fixtures_permission_request(READ_ONLY_CAP, capability.risk, capability.scope),
        granted_by="test",
    )
    result = await host.invoke(plugin, {})
    assert result.ok is False
    assert result.host_calls[0].decision == "undeclared"
    assert result.host_calls[0].state == "refused"
    assert capability.runs == 0
