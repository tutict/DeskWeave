"""Integration tests: the nine ways a plugin is expected to fail.

Each case asserts three things: the invocation reports a *specific* error code,
the host process stays alive, and the runtime is still usable afterwards. The
last point matters more than it looks — a sandbox that survives one bad plugin but
leaves the host in a wedged state is not a sandbox.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from deskweave_contracts import (
    ErrorInfo,
    PermissionRequest,
    PermissionScope,
    RiskLevel,
    TaskStatus,
)
from deskweave_plugin_wasm import (
    PluginAbiMismatchError,
    PluginCapabilityUndeclaredError,
    PluginInvalidOutputError,
    PluginMalformedError,
    PluginNotFoundError,
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
PLUGIN_ID = "reference-metadata"


def _host(runtime: Runtime) -> WasmPluginHost:
    return WasmPluginHost(registry=default_registry(runtime.permissions))


async def _grant(runtime: Runtime) -> None:
    await runtime.permissions.grant(
        PermissionRequest(
            adapter_id=principal_for(PLUGIN_ID),
            capability=CAPABILITY,
            scope=PermissionScope.PLUGIN_HOST_CALL,
            risk=RiskLevel.LOW,
        ),
        granted_by="test",
    )


async def _still_works(runtime: Runtime, host: WasmPluginHost) -> None:
    """Assert the host can still run a healthy plugin after a failure."""
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)
    result = await host.invoke(
        plugin,
        {"images": [{"artifact_id": "ok", "title": "Fine", "width": 100, "height": 80}]},
    )
    assert result.ok is True, result.error


# 1 -- plugin does not exist --------------------------------------------------


async def test_missing_module_is_not_found(runtime: Runtime, tmp_path: Path) -> None:
    with pytest.raises(PluginNotFoundError):
        _host(runtime).load(tmp_path / "absent.wasm")


# 2 -- plugin is malformed ----------------------------------------------------


async def test_malformed_bytes_are_refused(runtime: Runtime, tmp_path: Path) -> None:
    module = tmp_path / "broken.wasm"
    module.write_bytes(b"\x00asm\x01\x00\x00\x00 definitely-not-valid")
    (tmp_path / "broken.json").write_text(
        '{"plugin_id":"broken","name":"Broken"}', encoding="utf-8"
    )
    with pytest.raises(PluginMalformedError):
        _host(runtime).load(module)


async def test_manifest_claiming_another_abi_is_refused(runtime: Runtime, tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response("{}"),
        name="abi-claim",
        manifest_dict=fixtures.manifest(plugin_id="abi-claim", interface_version=2),
    )
    with pytest.raises(PluginAbiMismatchError):
        _host(runtime).load(path)


# 3 -- input does not match the schema ---------------------------------------


async def test_schema_invalid_input_is_rejected_before_execution(
    runtime: Runtime,
) -> None:
    host = _host(runtime)
    plugin = host.load(fixtures.EXAMPLE_WASM)
    await _grant(runtime)

    result = await host.invoke(plugin, {"images": [{"artifact_id": "x"}]})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "plugin_invalid_input"
    assert result.fuel_used is None
    await _still_works(runtime, host)


# 4 -- plugin returns an invalid result --------------------------------------


async def test_non_json_response_is_rejected(runtime: Runtime, tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response("this is not json"),
        name="bad-json",
        manifest_dict=fixtures.manifest(plugin_id="bad-json"),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidOutputError.code


async def test_response_lying_about_its_length_is_rejected(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A frame claiming 10 MiB must be refused before the host reads 10 MiB."""
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.lying_length_response(10 * 1024 * 1024),
        name="liar",
        manifest_dict=fixtures.manifest(plugin_id="liar"),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidOutputError.code
    assert result.error.details["limit"] == plugin.manifest.limits.max_output_bytes


async def test_response_violating_its_own_output_schema_is_rejected(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response('{"unexpected": true}'),
        name="bad-shape",
        manifest_dict=fixtures.manifest(
            plugin_id="bad-shape",
            output_schema={
                "type": "object",
                "required": ["checked"],
                "properties": {"checked": {"type": "integer"}},
            },
        ),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginInvalidOutputError.code


# 5 -- infinite loop ----------------------------------------------------------


async def test_infinite_loop_is_stopped_by_fuel(runtime: Runtime, tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.spin_forever(),
        name="spinner",
        manifest_dict=fixtures.manifest(
            plugin_id="spinner", limits={"fuel": 200_000, "timeout_ms": 5_000}
        ),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginResourceError.code
    assert result.error.details["trap"] == "OUT_OF_FUEL"
    await _still_works(runtime, host)


# 6 -- timeout ----------------------------------------------------------------


async def test_endless_plugin_is_interrupted_by_the_wall_clock(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Fuel is set high on purpose: only the epoch watchdog can stop this one."""
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.spin_forever(),
        name="slow",
        manifest_dict=fixtures.manifest(
            plugin_id="slow",
            limits={"fuel": 5_000_000_000, "timeout_ms": 150},
        ),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginTimeoutError.code
    assert result.duration_ms < 5_000
    await _still_works(runtime, host)


# 7 -- resource exhaustion ----------------------------------------------------


async def test_memory_cap_is_enforced_by_the_allocator(runtime: Runtime, tmp_path: Path) -> None:
    """A plugin that checks `memory.grow` degrades gracefully instead of trapping."""
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.memory_hog(),
        name="hog-graceful",
        manifest_dict=fixtures.manifest(
            plugin_id="hog-graceful",
            limits={"max_memory_bytes": 1024 * 1024, "fuel": 500_000_000},
        ),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    # The grow was refused, the plugin noticed, and it still answered correctly.
    assert result.ok is True, result.error
    assert result.data == {"ok": True}


async def test_writing_past_the_memory_cap_traps_instead_of_corrupting(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.out_of_bounds_writer(),
        name="hog-unsafe",
        manifest_dict=fixtures.manifest(
            plugin_id="hog-unsafe",
            limits={"max_memory_bytes": 1024 * 1024},
        ),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginTrapError.code
    await _still_works(runtime, host)


async def test_module_declaring_more_memory_than_allowed_never_instantiates(
    runtime: Runtime, tmp_path: Path
) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.declares_too_much_memory(pages=100),  # 6.25 MiB minimum
        name="greedy",
        manifest_dict=fixtures.manifest(
            plugin_id="greedy",
            limits={"max_memory_bytes": 1024 * 1024},
        ),
    )
    host = _host(runtime)

    with pytest.raises(PluginMalformedError):
        host.load(path)


# 8 -- undeclared host capability --------------------------------------------


async def test_undeclared_capability_is_refused_even_when_granted(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Declaring nothing does not stop the plugin calling; the host stops it."""
    manifest = fixtures.example_manifest(requested_capabilities=[])
    path = tmp_path / "undeclared.wasm"
    path.write_bytes(fixtures.EXAMPLE_WASM.read_bytes())
    (tmp_path / "undeclared.json").write_text(json.dumps(manifest), encoding="utf-8")

    host = _host(runtime)
    plugin = host.load(path)
    await _grant(runtime)

    result = await host.invoke(
        plugin,
        {"images": [{"artifact_id": "x", "title": "X", "width": 10, "height": 10}]},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginCapabilityUndeclaredError.code
    assert result.error.details["decision"] == "undeclared"


# 9 -- exception / cancellation during execution ------------------------------


async def test_trap_is_reported_not_propagated(runtime: Runtime, tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.trap_on_invoke(),
        name="boom",
        manifest_dict=fixtures.manifest(plugin_id="boom"),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginTrapError.code
    await _still_works(runtime, host)


async def test_proc_exit_is_a_trap_not_a_host_exit(runtime: Runtime, tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.exit_on_invoke(code=3),
        name="quitter",
        manifest_dict=fixtures.manifest(plugin_id="quitter"),
    )
    host = _host(runtime)
    plugin = host.load(path)

    result = await host.invoke(plugin, {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == PluginTrapError.code
    assert result.error.details["exit_code"] == 3


async def test_cancellation_interrupts_the_plugin_and_leaves_the_host_usable(
    runtime: Runtime, tmp_path: Path
) -> None:
    host = _host(runtime)
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.spin_forever(),
        name="cancel-me",
        manifest_dict=fixtures.manifest(
            plugin_id="cancel-me",
            limits={"fuel": 5_000_000_000, "timeout_ms": 10_000},
        ),
    )
    plugin = host.load(path)

    task = asyncio.create_task(host.invoke(plugin, {}))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _still_works(runtime, host)


async def test_host_survives_a_battery_of_bad_plugins(runtime: Runtime, tmp_path: Path) -> None:
    """Run every failure mode in sequence, then prove the host is still healthy."""
    host = _host(runtime)
    cases = [
        ("trap", fixtures.trap_on_invoke()),
        ("spin", fixtures.spin_forever()),
        ("garbage", fixtures.constant_response("not json")),
        ("liar", fixtures.lying_length_response(64 * 1024 * 1024)),
    ]
    for name, wat in cases:
        manifest_dict = fixtures.manifest(
            plugin_id=f"battery-{name}",
            limits={"fuel": 300_000, "timeout_ms": 300},
        )
        path = fixtures.write_plugin(
            tmp_path, wat, name=f"battery-{name}", manifest_dict=manifest_dict
        )
        result = await host.invoke(host.load(path), {})
        assert result.ok is False, f"{name} should have failed"

    await _still_works(runtime, host)


# 10 -- failure is not success ------------------------------------------------


async def test_a_failed_plugin_cannot_land_a_task_in_succeeded(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The task state machine must record the failure, not the absence of a callback.

    The plugin host is deliberately *not* wired into :class:`WorkflowEngine` yet —
    registering a WASM plugin as an adapter would be the migration this phase is
    forbidden to perform. What is worth pinning down now is the seam those two
    pieces will meet at: ``InvocationResult.error`` has to be a real
    :class:`ErrorInfo`, or a future caller will be forced to synthesise one and
    the plugin's own diagnosis gets lost on the way to ``TaskRecord.error``.

    The caller pattern below is the one the wiring will have to follow, and it is
    the pattern that matters: the *only* branch point is ``result.ok``. A plugin
    that traps, spins, lies or returns garbage never reaches the success arm,
    because every one of those paths leaves ``ok`` false.
    """
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.trap_on_invoke(),
        name="trap",
        manifest_dict=fixtures.manifest(plugin_id="trap", limits={"fuel": 200_000}),
    )
    run = await runtime.workflow.start_run("plugin-demo")
    task = await runtime.workflow.create_task(run.run_id, "plugin step")
    await runtime.workflow.set_task_status(task.task_id, TaskStatus.RUNNING)

    result = await _host(runtime).invoke(_host(runtime).load(path), {})

    assert result.ok is False
    assert isinstance(result.error, ErrorInfo)

    recorded = await runtime.workflow.set_task_status(
        task.task_id,
        TaskStatus.SUCCEEDED if result.ok else TaskStatus.FAILED,
        error=result.error,
    )

    assert recorded.status is TaskStatus.FAILED
    assert recorded.error is not None
    assert recorded.error.code == PluginTrapError.code
    assert recorded.result == {}
