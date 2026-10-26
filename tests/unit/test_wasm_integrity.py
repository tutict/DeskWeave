"""Install-time integrity: path rules, pinning, and what a hash is allowed to mean.

The tests are grouped by the claim they defend:

1. a declared path cannot leave the plugin directory (lexically or via a link);
2. a pin is written atomically and is idempotent;
3. a changed file is detected — whether it changed before loading or after install;
4. failures say *what* changed and never print the host's absolute paths.

There is deliberately no test asserting "a trusted plugin": SHA-256 cannot
establish authorship, and a test that implied otherwise would be the exact
overclaim this phase is trying to avoid.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import wasmtime

from deskweave_plugin_wasm import (
    INSTALL_RECORD_NAME,
    PluginInstallError,
    PluginIntegrityError,
    install,
    normalise_module_path,
    resolve_module_path,
    sha256_bytes,
    sha256_file,
    verify_install,
)
from support import wasm_plugins as fixtures

FILE_ID = "integrity-plugin"


def _plugin(
    directory: Path,
    *,
    capabilities: tuple[str, ...] = (),
    declare_hash: bool = True,
) -> tuple[Path, Path, str]:
    """Write a real plugin and return ``(module, manifest, digest)``."""
    module = fixtures.write_plugin(
        directory,
        fixtures.constant_response('{"ok": true}'),
        name="plugin",
        manifest_dict=fixtures.manifest(
            plugin_id=FILE_ID,
            requested_capabilities=list(capabilities),
        ),
    )
    manifest = directory / "plugin.json"
    digest = sha256_bytes(module.read_bytes())
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["wasm_path"] = "plugin.wasm"
    if declare_hash:
        payload["wasm_sha256"] = digest
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return module, manifest, digest


# -- 1. path rules -----------------------------------------------------------


@pytest.mark.parametrize(
    "declared",
    [
        "../escape.wasm",
        "sub/../../escape.wasm",
        "..\\escape.wasm",
        "./plugin.wasm",
        "",
        "   ",
    ],
)
def test_a_path_that_could_leave_the_directory_is_rejected(declared: str) -> None:
    """``..`` and empty components are refused as shapes, before any filesystem call."""
    with pytest.raises(PluginIntegrityError):
        normalise_module_path(declared, plugin_id=FILE_ID)


@pytest.mark.parametrize(
    "declared",
    [
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.wasm",
        "\\\\server\\share\\evil.wasm",
        "//server/share/evil.wasm",
    ],
)
def test_an_absolute_or_unc_path_is_rejected(declared: str) -> None:
    """An absolute path replaces the base directory entirely, so it is never "just a name"."""
    with pytest.raises(PluginIntegrityError):
        normalise_module_path(declared, plugin_id=FILE_ID)


def test_a_relative_path_inside_the_directory_resolves(tmp_path: Path) -> None:
    module, _manifest, _digest = _plugin(tmp_path)

    assert normalise_module_path("plugin.wasm") == "plugin.wasm"
    assert resolve_module_path(tmp_path, "plugin.wasm") == module.resolve()


def test_a_missing_module_is_reported_as_integrity_not_not_found(tmp_path: Path) -> None:
    """The manifest promises a file; the promise being false is an integrity failure."""
    (tmp_path / "plugin.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PluginIntegrityError) as caught:
        resolve_module_path(tmp_path, "plugin.wasm", plugin_id=FILE_ID)
    assert "does not exist" in str(caught.value)


def test_a_symlinked_module_is_refused(tmp_path: Path) -> None:
    """A link can point outside the directory, or rename bytes that were pinned."""
    real = tmp_path / "real.wasm"
    real.write_bytes(b"\x00asm\x01\x00\x00\x00")
    link = tmp_path / "linked.wasm"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("this platform cannot create symlinks for the test user")

    with pytest.raises(PluginIntegrityError) as caught:
        resolve_module_path(tmp_path, "linked.wasm", plugin_id=FILE_ID)
    assert "symbolic link" in str(caught.value)


# -- 2. pinning --------------------------------------------------------------


def test_install_writes_a_record_and_repeating_it_changes_nothing(tmp_path: Path) -> None:
    _module, manifest, digest = _plugin(tmp_path)

    outcome, record = install(manifest)
    assert outcome == "installed"
    assert record.wasm_sha256 == digest
    assert record.wasm_path == "plugin.wasm"
    assert (tmp_path / INSTALL_RECORD_NAME).is_file()

    again, second = install(manifest)
    assert again == "unchanged"
    assert second == record


def test_install_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """The atomic write must not leak its scratch file into the plugin directory."""
    _module, manifest, _digest = _plugin(tmp_path)

    install(manifest)

    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith(".install-")]
    assert leftovers == []


def test_install_refuses_a_changed_module_unless_replaced(tmp_path: Path) -> None:
    # No hash in the manifest here: this test is about the *pin* detecting a
    # change, not about a manifest that contradicts its own file.
    module, manifest, first = _plugin(tmp_path, declare_hash=False)
    install(manifest)

    # A different but still well-formed module: the interesting case is bytes that
    # would load fine, not a corrupt file the parser would reject anyway.
    module.write_bytes(wasmtime.wat2wasm(fixtures.constant_response('{"ok": false}')))

    with pytest.raises(PluginInstallError) as caught:
        install(manifest)
    assert caught.value.details["installed"]["sha256"] == first[:12]

    outcome, record = install(manifest, replace=True)
    assert outcome == "replaced"
    assert record.wasm_sha256 != first


def test_install_refuses_a_manifest_that_lies_about_the_hash(tmp_path: Path) -> None:
    _module, manifest, _digest = _plugin(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["wasm_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginInstallError) as caught:
        install(manifest)
    assert caught.value.details["declared"] == "0" * 64


# -- 3. detection ------------------------------------------------------------


def test_verify_detects_a_modified_module(tmp_path: Path) -> None:
    module, manifest, digest = _plugin(tmp_path)
    install(manifest)

    module.write_bytes(b"\x00asm\x01\x00\x00\x00" + b"\x00" * 8)

    with pytest.raises(PluginIntegrityError) as caught:
        verify_install(manifest)
    assert caught.value.details["expected"] == digest
    assert caught.value.details["found"] != digest


def test_verify_detects_a_manifest_that_added_a_capability(tmp_path: Path) -> None:
    """A pin fixes the declaration too: adding a capability afterwards is an escalation."""
    _module, manifest, _digest = _plugin(tmp_path, capabilities=("reference.metadata.validate",))
    install(manifest)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["requested_capabilities"] = [
        "reference.metadata.validate",
        "reference.metadata.delete",
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PluginIntegrityError) as caught:
        verify_install(manifest)
    assert caught.value.details["added"] == ["reference.metadata.delete"]


def test_verify_refuses_when_nothing_was_pinned(tmp_path: Path) -> None:
    _module, manifest, _digest = _plugin(tmp_path)

    with pytest.raises(PluginIntegrityError) as caught:
        verify_install(manifest)
    assert "not pinned" in str(caught.value)


def test_verify_returns_the_bytes_it_hashed(tmp_path: Path) -> None:
    """The caller must parse exactly these bytes; a second read could see a new file."""
    module, manifest, digest = _plugin(tmp_path)
    install(manifest)

    record, data = verify_install(manifest)

    assert record.wasm_sha256 == digest
    assert sha256_bytes(data) == digest
    assert data == module.read_bytes()


def test_the_streamed_hash_matches_the_in_memory_hash(tmp_path: Path) -> None:
    """``sha256_file`` is the install-time path and must agree with ``sha256_bytes``."""
    _module, _manifest, _digest = _plugin(tmp_path)
    module = tmp_path / "plugin.wasm"

    streamed, size = sha256_file(module)

    assert streamed == sha256_bytes(module.read_bytes())
    assert size == len(module.read_bytes())


# -- 4. what the errors may say ----------------------------------------------


def test_integrity_errors_do_not_leak_absolute_paths(tmp_path: Path) -> None:
    """An error message travels into logs and bug reports; the host's layout should not."""
    module, manifest, _digest = _plugin(tmp_path)
    install(manifest)
    module.write_bytes(b"\x00asm\x01\x00\x00\x00" + b"\x00" * 4)

    with pytest.raises(PluginIntegrityError) as caught:
        verify_install(manifest)

    rendered = str(caught.value) + json.dumps(caught.value.details, default=str)
    assert str(tmp_path) not in rendered
    assert caught.value.details["path"] == "plugin.wasm"
