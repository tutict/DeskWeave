"""Contract tests: what a module must look like to be loadable.

These assert on the *shape* of the compiled artifact rather than on its
behaviour, and they are the cheapest place to catch a host that accidentally
starts accepting something it should not — for example a module that imports a
filesystem function the sandbox is supposed to withhold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import wasmtime

from deskweave_plugin_wasm import (
    ABI_VERSION,
    PluginAbiMismatchError,
    PluginMalformedError,
    WasmPluginHost,
)
from deskweave_plugin_wasm.abi import (
    EXPORT_ABI_VERSION,
    EXPORT_ALLOC,
    EXPORT_FREE,
    EXPORT_INVOKE,
    EXPORT_MEMORY,
    IMPORT_MODULE,
)
from support import wasm_plugins as fixtures


def _module() -> wasmtime.Module:
    engine = wasmtime.Engine()
    return wasmtime.Module(engine, fixtures.EXAMPLE_WASM.read_bytes())


def test_example_artifact_is_committed() -> None:
    assert fixtures.EXAMPLE_WASM.is_file(), (
        "the example plugin must be built and committed so tests stay hermetic; "
        "run `uv run python scripts/build_wasm_plugin.py`"
    )
    assert fixtures.EXAMPLE_WASM.stat().st_size > 1024


def test_example_exports_every_required_name() -> None:
    exports = {export.name for export in _module().exports}
    assert {
        EXPORT_MEMORY,
        EXPORT_ABI_VERSION,
        EXPORT_ALLOC,
        EXPORT_FREE,
        EXPORT_INVOKE,
    } <= exports


def test_example_reports_the_current_abi_version() -> None:
    engine = wasmtime.Engine()
    module = wasmtime.Module(engine, fixtures.EXAMPLE_WASM.read_bytes())
    store = wasmtime.Store(engine)
    store.set_epoch_deadline(1)
    linker = wasmtime.Linker(engine)
    i32 = wasmtime.ValType.i32()
    linker.define_func(
        IMPORT_MODULE,
        "dw_host_call",
        wasmtime.FuncType([i32, i32, i32, i32], [i32]),
        lambda *_: 0,
        access_caller=True,
    )
    linker.define_wasi()
    store.set_wasi(wasmtime.WasiConfig())
    instance = linker.instantiate(store, module)
    assert instance.exports(store)[EXPORT_ABI_VERSION](store) == ABI_VERSION


def test_example_imports_nothing_but_the_host_call_and_wasi_stdlib() -> None:
    """The sandbox promise, checked against the artifact instead of the source.

    Rust's `std` links four WASI Preview 1 entry points regardless of what the
    program does; those stay. Anything that looks like a filesystem, socket or
    process import would mean the module was built against the wrong target.
    """
    allowed_wasi = {
        "environ_get",
        "environ_sizes_get",
        "fd_write",
        "proc_exit",
        "fd_close",
        "fd_read",
        "fd_seek",
        "fd_fdstat_get",
        "args_get",
        "args_sizes_get",
        "random_get",
        "clock_time_get",
    }
    imported = []
    for item in _module().imports:
        imported.append((item.module, item.name))
        if item.module == IMPORT_MODULE:
            assert item.name == "dw_host_call"
        elif item.module == "wasi_snapshot_preview1":
            assert item.name in allowed_wasi, f"unexpected WASI import {item.name}"
        else:
            pytest.fail(f"plugin imports an unapproved module: {item.module!r}")

    assert (IMPORT_MODULE, "dw_host_call") in imported


def test_module_without_entry_point_is_refused(tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.missing_entry_point(),
        name="no-entry",
        manifest_dict=fixtures.manifest(plugin_id="no-entry"),
    )
    with pytest.raises(PluginMalformedError) as excinfo:
        WasmPluginHost().load(path)
    assert "dw_invoke" in str(excinfo.value)


def test_manifest_interface_version_mismatch_is_refused(tmp_path: Path) -> None:
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response("{}"),
        name="bad-manifest-abi",
        manifest_dict=fixtures.manifest(plugin_id="bad-manifest-abi", interface_version=99),
    )
    with pytest.raises(PluginAbiMismatchError) as excinfo:
        WasmPluginHost().load(path)
    assert excinfo.value.details["expected"] == ABI_VERSION
    assert excinfo.value.details["found"] == 99


def test_module_reporting_another_abi_is_refused(tmp_path: Path) -> None:
    """A module can lie about the manifest; the exported version is the truth."""
    path = fixtures.write_plugin(
        tmp_path,
        fixtures.constant_response("{}", abi=7),
        name="bad-module-abi",
        manifest_dict=fixtures.manifest(plugin_id="bad-module-abi"),
    )
    with pytest.raises(PluginAbiMismatchError) as excinfo:
        WasmPluginHost().load(path)
    assert excinfo.value.details["found"] == 7


def test_garbage_bytes_are_not_loadable(tmp_path: Path) -> None:
    monkey = tmp_path / "garbage.wasm"
    monkey.write_bytes(b"this is definitely not webassembly")
    (tmp_path / "garbage.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PluginMalformedError):
        WasmPluginHost().load(monkey)


def test_missing_manifest_is_reported_separately(tmp_path: Path) -> None:
    module = tmp_path / "lonely.wasm"
    module.write_bytes(fixtures.EXAMPLE_WASM.read_bytes())
    with pytest.raises(Exception) as excinfo:
        WasmPluginHost().load(module)
    assert "manifest" in str(excinfo.value)
