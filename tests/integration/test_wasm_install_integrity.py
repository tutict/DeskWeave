"""Load-time integrity: what the host refuses before it runs a single instruction.

Three modes are covered, because they are three different promises:

* default — the hash is computed and recorded, nothing is enforced (backward
  compatible with every manifest written before this phase);
* ``require_integrity`` — the manifest must declare the hash and the bytes must
  match it;
* ``require_install_record`` — the bytes must match a pin written by
  ``install()``, and the manifest must not have grown capabilities since.

None of them proves who built the plugin. That is the point of
``docs/architecture/plugin-installation.md``, and these tests assert the limits
as well as the guarantees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import wasmtime

from deskweave_plugin_wasm import (
    PluginIntegrityError,
    PluginMalformedError,
    WasmPluginHost,
    install,
    sha256_bytes,
)
from support import wasm_plugins as fixtures

PLUGIN_ID = "integrity-host"


def _write(
    directory: Path,
    *,
    body: str = '{"ok": true}',
    declare_hash: bool = True,
    wasm_path: str | None = "plugin.wasm",
) -> tuple[Path, Path, str]:
    module = fixtures.write_plugin(
        directory,
        fixtures.constant_response(body),
        name="plugin",
        manifest_dict=fixtures.manifest(plugin_id=PLUGIN_ID),
    )
    manifest = directory / "plugin.json"
    digest = sha256_bytes(module.read_bytes())
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if wasm_path is not None:
        payload["wasm_path"] = wasm_path
    if declare_hash:
        payload["wasm_sha256"] = digest
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return module, manifest, digest


def _host(*, integrity: bool = False, record: bool = False) -> WasmPluginHost:
    # These tests model the *bytes* layer only: no approval, no lifecycle. A host
    # that pins bytes without asking who approved them is now refused by default,
    # so the omission has to be explicit — which is the point of the flag.
    return WasmPluginHost(
        require_integrity=integrity,
        require_install_record=record,
        allow_unguarded=True,
    )


# -- default mode stays compatible -------------------------------------------


def test_a_manifest_without_a_hash_still_loads_by_default(tmp_path: Path) -> None:
    """Every manifest written before Phase 2.5 has no hash; none of them may break."""
    module, _manifest, _digest = _write(tmp_path, declare_hash=False)

    plugin = WasmPluginHost().load(module)

    assert plugin.manifest.plugin_id == PLUGIN_ID
    assert len(plugin.digest) == 64


# -- require_integrity --------------------------------------------------------


def test_a_matching_hash_loads(tmp_path: Path) -> None:
    module, _manifest, digest = _write(tmp_path)

    plugin = _host(integrity=True).load(module)

    assert plugin.digest == digest


def test_a_module_replaced_after_the_manifest_was_written_is_refused(tmp_path: Path) -> None:
    """The core claim: swapping the file breaks the hash, and the host will not run it."""
    module, _manifest, _digest = _write(tmp_path)
    module.write_bytes(wasmtime.wat2wasm(fixtures.constant_response('{"ok": false}')))

    with pytest.raises(PluginIntegrityError) as caught:
        _host(integrity=True).load(module)

    assert "does not match the hash" in str(caught.value)
    assert caught.value.details["expected"] != caught.value.details["found"]


def test_a_manifest_without_a_hash_is_refused_when_integrity_is_required(tmp_path: Path) -> None:
    """ "No hash" must not be silently equivalent to "hash matched"."""
    module, _manifest, _digest = _write(tmp_path, declare_hash=False)

    with pytest.raises(PluginIntegrityError) as caught:
        _host(integrity=True).load(module)

    assert "declares no wasm_sha256" in str(caught.value)


def test_the_declared_module_must_be_the_loaded_module(tmp_path: Path) -> None:
    """A manifest that points elsewhere is a contradiction, not a search hint."""
    module, _manifest, _digest = _write(tmp_path)
    other = tmp_path / "other.wasm"
    other.write_bytes(module.read_bytes())
    payload = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8"))
    payload["wasm_path"] = "other.wasm"
    (tmp_path / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginIntegrityError) as caught:
        _host(integrity=True).load(module)

    assert "different file" in str(caught.value)


@pytest.mark.parametrize("declared", ["../escape.wasm", "C:\\Windows\\evil.wasm", "/etc/passwd"])
def test_a_manifest_with_a_traversing_path_is_refused_at_parse_time(
    tmp_path: Path, declared: str
) -> None:
    """Even before the filesystem is touched, the shape is rejected by the manifest model."""
    module, _manifest, _digest = _write(tmp_path)
    payload = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8"))
    payload["wasm_path"] = declared
    (tmp_path / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginMalformedError):
        _host(integrity=True).load(module)


# -- require_install_record ---------------------------------------------------


def test_an_unpinned_plugin_is_refused_when_a_record_is_required(tmp_path: Path) -> None:
    module, manifest, _digest = _write(tmp_path)

    with pytest.raises(PluginIntegrityError) as caught:
        _host(record=True).load(module)

    assert "not pinned" in str(caught.value)
    assert manifest.is_file()


def test_a_pinned_plugin_loads_and_a_later_swap_is_refused(tmp_path: Path) -> None:
    module, manifest, digest = _write(tmp_path)
    install(manifest)

    plugin = _host(record=True).load(module)
    assert plugin.digest == digest

    module.write_bytes(wasmtime.wat2wasm(fixtures.constant_response('{"ok": false}')))

    with pytest.raises(PluginIntegrityError) as caught:
        _host(record=True).load(module)
    assert "changed since it was pinned" in str(caught.value)


def test_loading_the_record_verifies_the_manifest_too(tmp_path: Path) -> None:
    """Version drift between manifest and pin is refused; an upgrade must re-pin."""
    module, manifest, _digest = _write(tmp_path)
    install(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "9.9.9"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginIntegrityError) as caught:
        _host(record=True).load(module)

    assert "version" in str(caught.value)


# -- the shipped example is pinned too ---------------------------------------


def test_the_example_plugin_matches_the_hash_in_its_manifest() -> None:
    """A committed artifact whose manifest hash drifts would teach the wrong habit."""
    module = fixtures.EXAMPLE_WASM
    payload = json.loads(fixtures.EXAMPLE_MANIFEST.read_text(encoding="utf-8"))

    assert payload["wasm_path"] == module.name
    assert payload["wasm_sha256"] == sha256_bytes(module.read_bytes())


def test_the_example_plugin_loads_with_integrity_required() -> None:
    plugin = WasmPluginHost(require_integrity=True).load(
        fixtures.EXAMPLE_WASM, fixtures.EXAMPLE_MANIFEST
    )

    assert plugin.manifest.plugin_id == "reference-metadata"
    assert plugin.digest == plugin.manifest.wasm_sha256


# -- what the failure may say -------------------------------------------------


def test_a_load_failure_does_not_print_the_host_directory(tmp_path: Path) -> None:
    module, _manifest, _digest = _write(tmp_path)
    module.write_bytes(wasmtime.wat2wasm(fixtures.constant_response('{"ok": false}')))

    with pytest.raises(PluginIntegrityError) as caught:
        _host(integrity=True).load(module)

    rendered = str(caught.value) + json.dumps(caught.value.details, default=str)
    assert str(tmp_path) not in rendered
