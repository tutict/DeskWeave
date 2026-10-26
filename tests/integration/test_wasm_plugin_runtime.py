"""Integration tests: the real Rust plugin through the real permission layer.

These are the acceptance-criteria tests for the prototype: load, invoke, validate
the result, and prove that authorisation comes from the host rather than from the
plugin's own manifest.
"""

from __future__ import annotations

from pathlib import Path

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    PluginCapabilityDeniedError,
    WasmPluginHost,
    default_registry,
    principal_for,
)
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

PLUGIN_ID = "reference-metadata"
CAPABILITY = "reference.metadata.validate"

GOOD_PAYLOAD = {
    "images": [
        {
            "artifact_id": "art-1",
            "title": "Thatched cottage",
            "width": 800,
            "height": 600,
            "license": "CC0",
            "author": "A. Photographer",
        },
        {
            "artifact_id": "art-2",
            "title": "   ",
            "width": 9000,
            "height": 100,
        },
    ]
}


def _host(runtime: Runtime) -> WasmPluginHost:
    return WasmPluginHost(registry=default_registry(runtime.permissions))


async def _grant(runtime: Runtime, capability: str = CAPABILITY) -> str:
    return await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN_ID),
            capability=capability,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="test",
    )


async def test_loads_the_committed_example(runtime: Runtime) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)

    assert plugin.manifest.plugin_id == PLUGIN_ID
    assert plugin.manifest.version == "0.1.0"
    assert plugin.manifest.declared_capabilities == frozenset({CAPABILITY})
    # The digest is of the bytes actually loaded, not of a path.
    assert len(plugin.digest) == 64


async def test_invocation_is_denied_without_a_grant(runtime: Runtime) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)

    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code
    # The denial is recorded even though the plugin turned it into an error body.
    assert [call.capability for call in result.host_calls] == [CAPABILITY]
    assert result.host_calls[0].decision in {"require_confirmation", "deny"}


async def test_invocation_succeeds_once_the_host_grants(runtime: Runtime) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert result.ok is True, result.error
    assert result.data is not None
    assert result.data["checked"] == 2
    assert [item["artifact_id"] for item in result.data["accepted"]] == ["art-1"]
    assert [item["artifact_id"] for item in result.data["rejected"]] == ["art-2"]
    assert result.data["rejected"][0]["problems"][0]["field"] == "title"


async def test_every_host_call_is_audited(runtime: Runtime) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert len(result.host_calls) == 2
    for call in result.host_calls:
        assert call.capability == CAPABILITY
        assert call.decision == "allow"
        assert call.duration_ms >= 0


async def test_fuel_is_consumed_and_bounded(runtime: Runtime) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert result.ok is True
    assert result.fuel_used is not None
    assert 0 < result.fuel_used < plugin.manifest.limits.fuel


async def test_two_invocations_do_not_share_memory(runtime: Runtime) -> None:
    """A fresh Store per call means state cannot leak between invocations."""
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    first = await host.invoke(plugin, GOOD_PAYLOAD)
    second = await host.invoke(plugin, GOOD_PAYLOAD)

    assert first.ok is True
    assert second.ok is True
    assert first.data == second.data


async def test_input_that_violates_the_schema_never_reaches_the_module(
    runtime: Runtime,
) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    result = await host.invoke(plugin, {"images": []})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "plugin_invalid_input"
    # No host call happened: the module was never started.
    assert result.host_calls == ()


async def test_grant_for_an_adapter_id_does_not_authorise_the_plugin(
    runtime: Runtime,
) -> None:
    """Principals are namespaced; a grant to `reference-metadata` is not a grant
    to `wasm-plugin:reference-metadata`."""
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=PLUGIN_ID,
            capability=CAPABILITY,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="test",
    )

    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code


async def test_manifest_path_can_be_supplied_explicitly(runtime: Runtime, tmp_path: Path) -> None:
    """The loader takes the manifest separately, so a host can pin its own copy."""
    host = _host(runtime)
    pinned = tmp_path / "pinned.json"
    pinned.write_text(fixtures.EXAMPLE_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")

    plugin = host.load(fixtures.EXAMPLE_WASM, manifest_path=pinned)
    await _grant(runtime)
    result = await host.invoke(plugin, GOOD_PAYLOAD)

    assert result.ok is True
