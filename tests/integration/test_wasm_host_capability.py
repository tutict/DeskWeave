"""Integration tests: the host-capability authorisation boundary.

The point of this file is to show that the decision lives on the host side. Every
test drives :class:`HostCapabilityRegistry` (and, where relevant, the whole
invocation) and asserts on the *decision*, not on the plugin's cooperation.
"""

from __future__ import annotations

import json
from pathlib import Path

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    HostCallStatus,
    PluginCapabilityDeniedError,
    WasmPluginHost,
    default_registry,
    principal_for,
)
from deskweave_runtime import Runtime
from deskweave_runtime.permissions import PermissionManager
from support import wasm_plugins as fixtures

CAPABILITY = "reference.metadata.validate"
PLUGIN_ID = "reference-metadata"

IMAGE = {
    "artifact_id": "art-1",
    "title": "Thatched cottage",
    "width": 800,
    "height": 600,
    "license": "CC0",
}


def _registry(manager: PermissionManager | None = None):
    return default_registry(manager)


async def test_unknown_capability_is_refused() -> None:
    registry = _registry(PermissionManager())

    outcome = await registry.dispatch(
        plugin_id="p",
        declared=frozenset({"reference.metadata.validate", "blender.delete_everything"}),
        capability="blender.delete_everything",
        payload={"image": IMAGE},
    )

    assert outcome.status == HostCallStatus.DENIED_UNKNOWN_CAPABILITY
    assert outcome.decision == "unknown"
    assert not outcome.ok


async def test_declaring_a_capability_is_not_a_grant() -> None:
    """The manifest said it wants this; the policy still says no."""
    registry = _registry(PermissionManager())

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset({CAPABILITY}),
        capability=CAPABILITY,
        payload={"image": IMAGE},
    )

    assert outcome.status == HostCallStatus.DENIED_NO_GRANT
    assert outcome.decision in {"require_confirmation", "deny"}


async def test_no_checker_means_deny() -> None:
    """An unwired host must not be a permissive one."""
    registry = _registry(None)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset({CAPABILITY}),
        capability=CAPABILITY,
        payload={"image": IMAGE},
    )

    assert outcome.status == HostCallStatus.DENIED_NO_GRANT
    assert "no permission checker" in outcome.reason


async def test_undeclared_capability_is_refused_before_the_checker_runs() -> None:
    registry = _registry(PermissionManager())

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset(),
        capability=CAPABILITY,
        payload={"image": IMAGE},
    )

    assert outcome.status == HostCallStatus.DENIED_NOT_DECLARED
    assert outcome.decision == "undeclared"


async def test_invalid_payload_is_refused_at_the_boundary() -> None:
    manager = PermissionManager()
    manager.allow_once(principal_for(PLUGIN_ID), CAPABILITY)
    registry = _registry(manager)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset({CAPABILITY}),
        capability=CAPABILITY,
        payload={"image": {"artifact_id": "x", "title": "ok", "width": "wide", "height": 1}},
    )

    assert outcome.status == HostCallStatus.INVALID_ARGUMENT
    assert outcome.decision == "invalid"


async def test_one_shot_grant_authorises_exactly_one_call() -> None:
    manager = PermissionManager()
    manager.allow_once(principal_for(PLUGIN_ID), CAPABILITY)
    registry = _registry(manager)
    kwargs = {
        "plugin_id": PLUGIN_ID,
        "declared": frozenset({CAPABILITY}),
        "capability": CAPABILITY,
        "payload": {"image": IMAGE},
    }

    first = await registry.dispatch(**kwargs)
    second = await registry.dispatch(**kwargs)

    assert first.ok
    assert first.data == {"valid": True, "problems": []}
    assert second.status == HostCallStatus.DENIED_NO_GRANT


async def test_response_is_minimal_and_does_not_echo_the_input() -> None:
    """A plugin must not be able to use the host as a mirror for secrets."""
    manager = PermissionManager()
    manager.allow_once(principal_for(PLUGIN_ID), CAPABILITY)
    registry = _registry(manager)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset({CAPABILITY}),
        capability=CAPABILITY,
        payload={
            "image": {
                **IMAGE,
                "license": "C:\\Users\\someone\\secret-token.txt",
                "author": "apikey=sk-live-1234567890",
            }
        },
    )

    assert outcome.ok
    assert outcome.data is not None
    encoded = json.dumps(outcome.data)
    assert "secret-token" not in encoded
    assert "sk-live" not in encoded
    assert set(outcome.data) == {"valid", "problems"}


async def test_problems_are_reported_without_passing_the_record_back() -> None:
    manager = PermissionManager()
    manager.allow_once(principal_for(PLUGIN_ID), CAPABILITY)
    registry = _registry(manager)

    outcome = await registry.dispatch(
        plugin_id=PLUGIN_ID,
        declared=frozenset({CAPABILITY}),
        capability=CAPABILITY,
        payload={"image": {**IMAGE, "title": "   "}},
    )

    assert outcome.ok
    assert outcome.data is not None
    assert outcome.data["valid"] is False
    assert outcome.data["problems"] == [{"field": "title", "reason": "must be non-empty text"}]
    assert "Thatched" not in json.dumps(outcome.data)


async def test_plugin_that_swallows_a_denial_cannot_report_success(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The host's view of the call outranks the plugin's own summary."""
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.calls_host_then_claims_success(
            json.dumps({"capability": CAPABILITY, "payload": {"image": IMAGE}})
        ),
        name="liar",
        manifest_dict=fixtures.manifest(
            plugin_id="liar",
            requested_capabilities=[CAPABILITY],
            output_schema={
                "type": "object",
                "required": ["checked", "accepted", "rejected"],
                "properties": {
                    "checked": {"type": "integer"},
                    "accepted": {"type": "array"},
                    "rejected": {"type": "array"},
                },
            },
        ),
    )
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    plugin = host.load(path)

    # Note: no grant. The module returns a perfectly well-formed success payload.
    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code
    assert result.error.details["plugin_id"] == "liar"
    # The audit shows the denial even though the plugin hid it.
    assert [call.decision for call in result.host_calls] == ["require_confirmation"]


async def test_end_to_end_capability_call_is_audited(runtime: Runtime) -> None:
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN_ID),
            capability=CAPABILITY,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="test",
    )

    result = await host.invoke(plugin, {"images": [IMAGE]})

    assert result.ok is True, result.error
    assert len(result.host_calls) == 1
    audit = result.host_calls[0]
    assert audit.capability == CAPABILITY
    assert audit.decision == "allow"
    assert audit.status == 0
