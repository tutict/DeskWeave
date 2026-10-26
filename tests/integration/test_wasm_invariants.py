"""Tests: the six invariants the sandbox has to satisfy.

These are not feature tests. Each one states a property that must hold for *any*
plugin, including a hostile one, and then tries to break it:

1. a failing plugin is never reported as a success;
2. an error never carries a token, an absolute host path or unauthorised data;
3. one invocation cannot leave state behind for the next;
4. a failing plugin cannot skip the permission check;
5. every declared resource limit is enforced by a real run;
6. an operation that cannot be cancelled is recorded as a risk, not assumed away.

Invariant 6 has no analogue here on purpose: after Phase 2.4 a synchronous
capability is offloaded to a bounded pool, so it no longer freezes the loop, but
it still cannot be interrupted. The evidence is a measurement rather than an
assertion, so it lives in ``test_wasm_host_execution.py``
(``test_a_sync_capability_times_out_and_keeps_running``,
``test_cancelling_the_invocation_does_not_claim_a_sync_capability_stopped``) and
in the threat model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from deskweave_contracts import ErrorInfo, PermissionRequest, PermissionScope, RiskLevel
from deskweave_plugin_wasm import (
    PluginCapabilityDeniedError,
    PluginInvalidInputError,
    PluginInvalidOutputError,
    PluginMalformedError,
    PluginResourceError,
    PluginTimeoutError,
    PluginTrapError,
    WasmPluginHost,
    default_registry,
    principal_for,
)
from deskweave_runtime import Runtime
from support import wasm_plugins as fixtures

CAPABILITY = "reference.metadata.validate"
PLUGIN_ID = "invariant"
IMAGE = {"artifact_id": "art-1", "title": "Cottage", "width": 800, "height": 600}
REQUEST = json.dumps({"capability": CAPABILITY, "payload": {"image": IMAGE}})

#: Patterns that must never appear in anything the host hands back.
SECRET_PATTERNS = [
    re.compile(r"[A-Za-z]:\\\\"),  # C:\...
    re.compile(r"[A-Za-z]:\\"),  # C:\... (single backslash in the raw string)
    re.compile(r"/home/"),
    re.compile(r"/Users/"),
    re.compile(r"sk-live"),
    re.compile(r"Bearer\s+[A-Za-z0-9]"),
    re.compile(r"token", re.IGNORECASE),
]


async def _grant(runtime: Runtime, plugin_id: str = PLUGIN_ID) -> None:
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(plugin_id),
            capability=CAPABILITY,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="invariant-test",
    )


def _plugin(tmp_path: Path, wat: str, *, plugin_id: str = PLUGIN_ID, **limits) -> Path:
    return fixtures.write_plugin(
        tmp_path,
        wat,
        name=plugin_id,
        manifest_dict=fixtures.manifest(
            plugin_id=plugin_id,
            requested_capabilities=[CAPABILITY],
            limits=limits,
        ),
    )


# -- 1: failure is never success ----------------------------------------------


@pytest.mark.parametrize(
    ("label", "wat", "limits", "expected_code"),
    [
        ("trap", lambda: fixtures.trap_on_invoke(), {}, PluginTrapError.code),
        (
            "spin-fuel",
            lambda: fixtures.spin_forever(),
            {"fuel": 200_000},
            PluginResourceError.code,
        ),
        (
            "spin-deadline",
            lambda: fixtures.spin_forever(),
            {"fuel": 5_000_000_000, "timeout_ms": 150},
            PluginTimeoutError.code,
        ),
        (
            "garbage-json",
            lambda: fixtures.constant_response("not json"),
            {},
            PluginInvalidOutputError.code,
        ),
        (
            "liar-length",
            lambda: fixtures.lying_length_response(8 * 1024 * 1024),
            {},
            PluginInvalidOutputError.code,
        ),
        (
            "bad-alloc",
            lambda: fixtures.alloc_beyond_memory(-1),
            {},
            PluginResourceError.code,
        ),
        (
            "bad-out-buffer",
            lambda: fixtures.host_request_bad_out_ptr(REQUEST, -1),
            {},
            PluginInvalidInputError.code,
        ),
        (
            "deep-json",
            lambda: fixtures.nested_host_request(60_000),
            {"max_input_bytes": 1 << 20},
            PluginInvalidInputError.code,
        ),
    ],
)
async def test_failure_is_never_reported_as_success(
    runtime: Runtime, tmp_path: Path, label: str, wat, limits: dict, expected_code: str
) -> None:
    path = _plugin(tmp_path, wat(), plugin_id=label, **limits)
    await _grant(runtime, plugin_id=label)
    host = WasmPluginHost(registry=default_registry(runtime.permissions))

    result = await host.invoke(host.load(path), {})

    assert result.ok is False, label
    assert result.error is not None, label
    assert isinstance(result.error, ErrorInfo), label
    assert result.error.code == expected_code, (label, result.error.code)
    # A failed invocation carries no payload a caller could mistake for output.
    assert result.data is None, label


# -- 2: no secrets in what comes back -----------------------------------------


async def test_errors_never_leak_paths_or_tokens(runtime: Runtime, tmp_path: Path) -> None:
    """A failing plugin's error is inspected, not trusted to be tidy."""
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    cases: list[ErrorInfo] = []

    # 1. a module that does not exist — the classic place a full path leaks
    with pytest.raises(Exception) as missing:
        host.load(tmp_path / "does-not-exist.wasm")
    cases.append(missing.value.to_info())

    # 2. a module that exists but whose manifest is broken
    broken = _plugin(tmp_path, fixtures.constant_response("{}"), plugin_id="broken")
    broken.with_suffix(".json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(Exception) as malformed:
        host.load(broken)
    cases.append(malformed.value.to_info())

    # 3. a well-formed plugin that fails at run time
    failing = _plugin(tmp_path, fixtures.trap_on_invoke(), plugin_id="trap")
    result = await host.invoke(host.load(failing), {})
    assert result.error is not None
    cases.append(result.error)

    for info in cases:
        rendered = json.dumps(info.model_dump(mode="json"))
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(rendered), (pattern.pattern, rendered)
        # The temp directory the test runs in is an absolute host path; the error
        # may name the file, never the directory it lives in.
        assert str(tmp_path) not in rendered


# -- 3: no state carries over --------------------------------------------------


async def test_a_failed_invocation_leaves_nothing_behind(runtime: Runtime, tmp_path: Path) -> None:
    """Failure, then a different plugin: the second runs on its own terms."""
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    bad = host.load(_plugin(tmp_path, fixtures.trap_on_invoke(), plugin_id="bad"))
    good = host.load(fixtures.EXAMPLE_WASM)

    failed = await host.invoke(bad, {})
    assert failed.ok is False

    await _grant(runtime, plugin_id="reference-metadata")
    healthy = await host.invoke(
        good,
        {"images": [IMAGE]},
    )

    assert healthy.ok is True, healthy.error
    assert len(healthy.host_calls) == 1


async def test_memory_does_not_survive_between_invocations(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Fresh ``Store`` per call: a plugin cannot stash a capability result for later."""
    path = _plugin(tmp_path, fixtures.host_request(REQUEST))
    await _grant(runtime)
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    plugin = host.load(path)

    first = await host.invoke(plugin, {})
    second = await host.invoke(plugin, {})

    assert first.ok is True, first.error
    assert second.ok is True, second.error
    # Each invocation dispatched exactly one host call of its own; neither saw
    # the other's grant consumption or audit trail.
    assert len(first.host_calls) == 1
    assert len(second.host_calls) == 1


# -- 4: failure does not bypass authorisation ---------------------------------


async def test_a_failing_plugin_still_goes_through_the_permission_check(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The plugin traps *after* a denied call; the denial is what gets reported."""
    path = _plugin(tmp_path, fixtures.host_request(REQUEST), plugin_id="denied-then-noisy")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code
    # The permission manager was consulted: the decision is recorded.
    assert result.host_calls[0].decision in {"deny", "require_confirmation"}


async def test_a_denied_plugin_cannot_retry_its_way_to_a_grant(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = _plugin(tmp_path, fixtures.repeated_host_calls(5, REQUEST), plugin_id="retrier")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))

    result = await host.invoke(host.load(path), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityDeniedError.code
    assert all(not call.ok for call in result.host_calls)


# -- 5: every limit is enforced by a real run ----------------------------------


@pytest.mark.parametrize(
    ("limit", "wat", "settings", "expected_code"),
    [
        ("fuel", lambda: fixtures.spin_forever(), {"fuel": 150_000}, PluginResourceError.code),
        (
            "timeout_ms",
            lambda: fixtures.spin_forever(),
            {"fuel": 5_000_000_000, "timeout_ms": 120},
            PluginTimeoutError.code,
        ),
        (
            "max_memory_bytes",
            lambda: fixtures.declares_too_much_memory(100),
            {"max_memory_bytes": 1 << 20},
            PluginMalformedError.code,
        ),
        (
            "max_output_bytes",
            lambda: fixtures.constant_response(json.dumps({"blob": "x" * 4096})),
            {"max_output_bytes": 64},
            PluginInvalidOutputError.code,
        ),
        (
            "max_host_calls",
            lambda: fixtures.repeated_host_calls(20, REQUEST),
            {"max_host_calls": 2},
            PluginResourceError.code,
        ),
    ],
)
async def test_each_declared_limit_is_enforced(
    runtime: Runtime, tmp_path: Path, limit: str, wat, settings: dict, expected_code: str
) -> None:
    """A limit that is only written in a config file is not a limit."""
    path = _plugin(tmp_path, wat(), plugin_id=f"limit-{limit}", **settings)
    await _grant(runtime, plugin_id=f"limit-{limit}")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))

    try:
        plugin = host.load(path)
    except Exception as exc:  # the memory ceiling is enforced at load
        assert getattr(exc, "code", None) == expected_code, (limit, exc)
        return

    result = await host.invoke(plugin, {})

    assert result.ok is False, limit
    assert result.error is not None, limit
    assert result.error.code == expected_code, (limit, result.error.code)


async def test_a_table_cannot_be_larger_than_the_memory_budget(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A table is memory too, and ``memory_size`` alone does not bound it.

    ``Store.set_limits`` leaves every field it is not given *unlimited*. Setting
    only ``memory_size`` therefore bounded pages: a module could declare
    20 000 000 funcref — 160 MB of references — never call ``memory.grow``, and
    be instantiated under a 1 MiB cap. Measured before the fix: such a module
    loaded successfully and pushed the process to ~220 MB.

    The bound is derived from the same budget rather than added as a second
    knob: one entry is one reference, so the bytes that buy pages buy slots.
    """
    path = _plugin(
        tmp_path,
        fixtures.declares_too_large_a_table(4_000_000),
        plugin_id="big-table",
        max_memory_bytes=1 << 20,
    )
    await _grant(runtime, plugin_id="big-table")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))

    with pytest.raises(PluginMalformedError) as caught:
        host.load(path)

    # Named, not just typed: "malformed" also covers a missing export, and the
    # point of this test is which limit refused it.
    assert "table" in str(caught.value).lower(), caught.value


async def test_a_module_cannot_declare_a_second_linear_memory(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``memory_size`` is a *per-memory* allowance, so the count has to be pinned.

    Two memories growing to the declared cap each is twice the budget, spent with
    no ``table`` trick and no ``memory.grow`` on the exported memory.
    """
    wat = fixtures.declares_two_memories()
    path = _plugin(tmp_path, wat, plugin_id="two-memories", max_memory_bytes=1 << 20)
    await _grant(runtime, plugin_id="two-memories")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))

    with pytest.raises(PluginMalformedError) as caught:
        host.load(path)

    assert "memory" in str(caught.value).lower(), caught.value


async def test_input_limit_is_enforced_before_the_module_runs(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = _plugin(tmp_path, fixtures.constant_response("{}"), plugin_id="big-input")
    host = WasmPluginHost(registry=default_registry(runtime.permissions))
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    manifest["limits"]["max_input_bytes"] = 8
    path.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")

    result = await host.invoke(host.load(path), {"payload": "x" * 4096})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidInputError.code
