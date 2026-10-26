"""Adversarial tests: the host-capability boundary, attacked.

Every test here builds a *hostile* module and drives the real
:class:`WasmPluginHost` against the real :class:`PermissionManager`. Nothing is
asserted from logs: each case checks the returned :class:`InvocationResult`,
which is the only thing a caller sees.

The point is not that hostile plugins are stopped — most of them are supposed to
be stopped — but that being stopped produces a *defined* outcome. A sandbox whose
weakness is "the host raises ``IndexError``" has no boundary at all, because the
caller cannot distinguish that from its own bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskweave_contracts import PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    PluginCapabilityDeniedError,
    PluginCapabilityUndeclaredError,
    PluginError,
    PluginInvalidInputError,
    PluginMalformedError,
    PluginResourceError,
    WasmPluginHost,
    default_registry,
    principal_for,
)
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

CAPABILITY = "reference.metadata.validate"
PLUGIN_ID = "adversary"

GOOD_IMAGE = {"artifact_id": "art-1", "title": "Cottage", "width": 800, "height": 600}


def _host(runtime: Runtime) -> WasmPluginHost:
    return WasmPluginHost(registry=default_registry(runtime.permissions))


async def _grant(
    runtime: Runtime, plugin_id: str = PLUGIN_ID, capability: str = CAPABILITY
) -> None:
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(plugin_id),
            capability=capability,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="adversarial-test",
    )


def _plugin(
    tmp_path: Path,
    wat: str,
    *,
    plugin_id: str = PLUGIN_ID,
    capabilities: list[str] | None = None,
    limits: dict | None = None,
) -> Path:
    return fixtures.write_plugin(
        tmp_path,
        wat,
        name=plugin_id,
        manifest_dict=fixtures.manifest(
            plugin_id=plugin_id,
            requested_capabilities=capabilities if capabilities is not None else [CAPABILITY],
            limits=limits or {},
        ),
    )


def _valid_request(**overrides) -> str:
    payload = {"image": {**GOOD_IMAGE, **overrides}}
    return json.dumps({"capability": CAPABILITY, "payload": payload})


# 1 -- capability selection ----------------------------------------------------


async def test_unregistered_capability_is_refused(runtime: Runtime, tmp_path: Path) -> None:
    """A name that this host does not offer at all."""
    request = json.dumps({"capability": "blender.delete_scene", "payload": {}})
    path = _plugin(
        tmp_path,
        fixtures.host_request(request),
        capabilities=[CAPABILITY, "blender.delete_scene"],
    )
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    # Declared, granted-by-nobody, but unknown to the host: the registry answers
    # before the permission layer is consulted.
    assert result.error.code == PluginCapabilityDeniedError.code
    assert result.host_calls[0].decision == "unknown"


async def test_registered_but_ungranted_capability_is_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()))
    # No grant at all.
    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code
    assert result.error.details["decision"] in {"deny", "require_confirmation"}


async def test_declaring_a_capability_does_not_authorise_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The manifest asks; the policy decides. Same request, one difference."""
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()))
    host = _host(runtime)

    denied = await host.invoke(host.load(path), {})

    await _grant(runtime)
    allowed = await host.invoke(host.load(path), {})

    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.code == PluginCapabilityDeniedError.code
    assert allowed.ok is True, allowed.error


async def test_grant_for_a_different_plugin_does_not_transfer(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Grants are per principal, not per capability name."""
    await _grant(runtime, plugin_id="someone-else")
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()))

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code


async def test_undeclared_capability_is_refused_even_though_it_is_granted(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Declaration is necessary and never sufficient — and granting is not declaring."""
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()), capabilities=[])
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityUndeclaredError.code


# 2 -- request forgery --------------------------------------------------------

MALFORMED_REQUESTS = {
    "empty": b"",
    "not-json": b"hello",
    "truncated": b'{"capability": "reference.metadata.validate"',
    "invalid-utf8": b'{"capability": "reference.metadata.validate", "payload": {"x": "\xff\xfe"}}',
    "json-array": b"[1, 2, 3]",
    "json-string": b'"just a string"',
    "json-null": b"null",
    "capability-not-string": b'{"capability": 42, "payload": {}}',
    "payload-not-object": b'{"capability": "reference.metadata.validate", "payload": []}',
    "missing-payload": b'{"capability": "reference.metadata.validate"}',
    "missing-capability": b'{"payload": {"image": {}}}',
}


@pytest.mark.parametrize("label", sorted(MALFORMED_REQUESTS))
async def test_malformed_request_is_refused_and_audited(
    runtime: Runtime, tmp_path: Path, label: str
) -> None:
    """Every shape of garbage gets the same defined answer, and the plugin cannot hide it."""
    raw = MALFORMED_REQUESTS[label]
    path = _plugin(tmp_path, fixtures.raw_host_request(raw))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    # The module ignores the host-call status and returns {"ok": true}; the host
    # must still fail the invocation, because it saw a contract violation.
    assert result.ok is False, label
    assert result.error is not None, label
    assert result.error.code in {
        PluginInvalidInputError.code,
        PluginCapabilityDeniedError.code,
    }, label
    assert result.host_calls, label


async def test_deeply_nested_request_is_refused_not_crashed(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A document deep enough to make the JSON *decoder* recurse.

    ``RecursionError`` is not a ``ValueError``, so this is the case that used to
    unwind straight through the host and out of ``invoke``.
    """
    path = _plugin(
        tmp_path,
        fixtures.nested_host_request(60_000),
        limits={"fuel": 500_000_000, "max_input_bytes": 1024 * 1024},
    )
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code
    assert "JSON" in result.error.message


async def test_request_length_beyond_the_limit_is_refused(runtime: Runtime, tmp_path: Path) -> None:
    """``req_len`` above ``max_input_bytes`` never reaches the decoder."""
    request = _valid_request()
    path = _plugin(
        tmp_path,
        fixtures.host_request(request),
        limits={"max_input_bytes": 16},
    )
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code
    assert "outside the allowed range" in result.error.message


async def test_request_pointer_outside_memory_is_refused(runtime: Runtime, tmp_path: Path) -> None:
    path = _plugin(tmp_path, fixtures.host_request_bad_req_ptr(_valid_request()))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code


async def test_extra_payload_fields_are_rejected_by_the_capability_schema(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The capability declares ``additionalProperties: false`` and means it."""
    request = json.dumps(
        {
            "capability": CAPABILITY,
            "payload": {"image": {**GOOD_IMAGE, "shell": "/bin/sh"}},
        }
    )
    path = _plugin(tmp_path, fixtures.host_request(request))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.host_calls[0].decision == "invalid"


async def test_unknown_envelope_keys_are_ignored_not_honoured(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Records current behaviour: extra *envelope* keys are dropped, not obeyed.

    The envelope is ``{capability, payload}``. An extra key such as
    ``"admin": true`` is ignored — it neither grants anything nor fails the call —
    which is worth pinning down explicitly: "ignored" is safe only because the
    envelope carries no authority in the first place.
    """
    request = json.dumps(
        {
            "capability": CAPABILITY,
            "payload": {"image": GOOD_IMAGE},
            "admin": True,
            "grant": ["*"],
        }
    )
    path = _plugin(tmp_path, fixtures.host_request(request))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is True, result.error
    assert result.data == {"ok": True}
    assert result.host_calls[0].decision == "allow"


async def test_capability_schema_still_rejects_bad_arguments(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A *well-formed* request with a payload the capability's schema rejects."""
    request = json.dumps(
        {
            "capability": CAPABILITY,
            "payload": {"image": {**GOOD_IMAGE, "width": "wide"}},
        }
    )
    path = _plugin(tmp_path, fixtures.host_request(request))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.host_calls[0].decision == "invalid"


# 3 -- output-buffer attacks --------------------------------------------------


async def test_writable_buffer_at_a_negative_pointer_is_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``out_ptr = -1`` used to raise ``IndexError`` out of ``invoke``."""
    path = _plugin(tmp_path, fixtures.host_request_bad_out_ptr(_valid_request(), -1))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code
    assert "outside linear memory" in result.error.message


async def test_output_capacity_smaller_than_the_response_is_refused(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The plugin offers less room than the answer needs, then claims success."""
    path = _plugin(tmp_path, fixtures.host_request_bad_out_ptr(_valid_request(), 200000, 8))
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code


async def test_alloc_pointer_outside_memory_is_a_plugin_error(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``dw_alloc`` is guest code: its return value is not an address the host may trust.

    Reachable with no grant and no host call at all — the request buffer is
    written before anything else happens.
    """
    for ptr in (-1, 0x7FFF_FFF0):
        path = _plugin(tmp_path, fixtures.alloc_beyond_memory(ptr), plugin_id=f"alloc-{ptr}")
        result = await _host(runtime).invoke(_host(runtime).load(path), {})

        assert result.ok is False, ptr
        assert result.error is not None, ptr
        assert result.error.code == PluginResourceError.code, ptr


# 4 -- interface tampering ----------------------------------------------------


async def test_manifest_interface_version_cannot_be_raised(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = _plugin(tmp_path, fixtures.constant_response('{"ok": true}'))
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    manifest["interface_version"] = 99
    path.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PluginError) as caught:
        _host(runtime).load(path)

    assert caught.value.code == "plugin_abi_mismatch"


async def test_module_reporting_a_foreign_abi_is_refused(runtime: Runtime, tmp_path: Path) -> None:
    path = _plugin(tmp_path, fixtures.constant_response('{"ok": true}', abi=7))

    with pytest.raises(PluginError) as caught:
        _host(runtime).load(path)

    assert caught.value.code == "plugin_abi_mismatch"


@pytest.mark.parametrize("export", ["dw_abi_version", "dw_alloc", "dw_invoke", "dw_invoke_void"])
async def test_abi_signature_mismatch_is_a_load_error(
    runtime: Runtime, tmp_path: Path, export: str
) -> None:
    """A wrong signature must be caught at load, not as a ``TypeError`` at call time."""
    path = _plugin(tmp_path, fixtures.wrong_signature(export), plugin_id="sig")

    with pytest.raises(PluginMalformedError) as caught:
        _host(runtime).load(path)

    assert caught.value.code == PluginMalformedError.code


async def test_trap_during_instantiation_is_a_load_error(runtime: Runtime, tmp_path: Path) -> None:
    """Instantiation runs guest code (``start``), and a trap is not a ``WasmtimeError``."""
    path = _plugin(tmp_path, fixtures.trapping_start(), plugin_id="start-trap")

    with pytest.raises(PluginMalformedError):
        _host(runtime).load(path)


async def test_trap_in_abi_version_is_a_load_error(runtime: Runtime, tmp_path: Path) -> None:
    path = _plugin(tmp_path, fixtures.trapping_abi_version(), plugin_id="abi-trap")

    with pytest.raises(PluginMalformedError):
        _host(runtime).load(path)


# 5 -- repeated and retried use ------------------------------------------------


async def test_many_host_calls_are_capped(runtime: Runtime, tmp_path: Path) -> None:
    """A loop of host calls is bounded by ``max_host_calls``, not by fuel."""
    path = _plugin(
        tmp_path,
        fixtures.repeated_host_calls(200, _valid_request()),
        limits={"max_host_calls": 10, "fuel": 500_000_000},
    )
    await _grant(runtime)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginResourceError.code
    assert result.error.details["budget"] == 10
    # Nine dispatches plus the single budget record — not 200.
    assert len(result.host_calls) <= 11


async def test_a_failed_invocation_leaves_no_state_for_the_next(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Same plugin, twice: the second run must be evaluated on its own merits."""
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()))
    host = _host(runtime)
    plugin = host.load(path)

    first = await host.invoke(plugin, {})
    second = await host.invoke(plugin, {})

    assert first.ok is False
    assert second.ok is False
    # Both fail for their own reason, and neither inherits the other's audit.
    assert len(second.host_calls) == 1

    await _grant(runtime)
    third = await host.invoke(plugin, {})

    assert third.ok is True, third.error
    assert len(third.host_calls) == 1


async def test_a_one_shot_grant_is_spent_by_the_first_invocation(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Retrying after a success must not reuse an allowance."""
    path = _plugin(tmp_path, fixtures.host_request(_valid_request()))
    host = _host(runtime)
    plugin = host.load(path)
    runtime.permissions.allow_once(principal_for(PLUGIN_ID), CAPABILITY)

    first = await host.invoke(plugin, {})
    second = await host.invoke(plugin, {})

    assert first.ok is True, first.error
    assert second.ok is False
    assert second.error is not None
    assert second.error.code == PluginCapabilityDeniedError.code
