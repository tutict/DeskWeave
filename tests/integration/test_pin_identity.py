"""Phase 2.6.7b, B: uninstall deletes only the pin it wrote.

These tests never close the lifecycle and never race anything: the store lock is
held throughout, so they prove the pin rule on its own. That separation is the
point — a longer lock lifetime does not stop a non-cooperating writer, and a test
that passed only because the lock was held would not show that.

Identity is ``(plugin_id, wasm_sha256)`` read from the file itself. A pin's
``version`` field is metadata, so doctoring it alone is *not* a replacement: the
file still describes the same install and is still this operation's to delete.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import INSTALL_RECORD_NAME, sha256_file
from deskweave_plugin_wasm.lifecycle import InstallState, JsonApprovalStore, PluginLifecycle
from support import wasm_plugins as fixtures

VALID = "reference.metadata.validate"
PLUGIN = "pin-identity"
LOGGER = "deskweave.wasm.lifecycle"


class QuietAuthority:
    """Authority double with a real grant table and no gating."""

    def __init__(self) -> None:
        self.granted: dict[str, set[str]] = {}

    async def list_effective_grants(self) -> list[dict[str, Any]]:
        return [
            {"adapter_id": principal, "capability": capability}
            for principal, capabilities in self.granted.items()
            for capability in sorted(capabilities)
        ]

    async def grant(self, request: Any, **kwargs: Any) -> str:
        self.granted.setdefault(str(request.adapter_id), set()).add(str(request.capability))
        return "granted"

    async def revoke(
        self, principal: str, capabilities: Any = None, **kwargs: Any
    ) -> tuple[str, ...]:
        held = self.granted.get(principal, set())
        if capabilities is None:
            removed = sorted(held)
            self.granted.pop(principal, None)
        else:
            removed = sorted(held.intersection(str(c) for c in capabilities))
            self.granted[principal] = held - set(removed)
        return tuple(f"{principal}:{capability}" for capability in removed)


def _manifest(directory: Path) -> Path:
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        directory, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
    )
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=PLUGIN,
        wasm_path=module.name,
        wasm_sha256=digest,
        version="1.0.0",
        requested_capabilities=[VALID],
        input_schema={},
        output_schema={},
    )
    manifest = module.with_suffix(".json")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


async def _approved(directory: Path) -> tuple[PluginLifecycle, QuietAuthority, Path, Path]:
    authority = QuietAuthority()
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None),
        permissions=authority,
        store=JsonApprovalStore(directory / "approvals.json"),
    )
    manifest = _manifest(directory)
    await lifecycle.install(manifest)
    await lifecycle.approve(PLUGIN, [VALID])
    return lifecycle, authority, directory / "approvals.json", manifest.parent / INSTALL_RECORD_NAME


def _state(store: Path) -> str:
    return json.loads(store.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"]


def _replacement(plugin_id: str, digest: str) -> bytes:
    """A valid pin for a *different* install."""
    return (
        json.dumps(
            {
                "record_version": 1,
                "plugin_id": plugin_id,
                "version": "9.9.9",
                "interface_version": 1,
                "wasm_path": "someone-else.wasm",
                "wasm_sha256": digest,
                "size_bytes": 1024,
                "declared_capabilities": [VALID],
            },
            indent=2,
            sort_keys=True,
        )
    ).encode()


async def test_a_replaced_pin_is_kept_and_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pin for another install survives an uninstall that decided about ours."""
    lifecycle, _authority, store, pin = await _approved(tmp_path)
    try:
        replacement = _replacement(f"{PLUGIN}-other", "a" * 64)
        pin.write_bytes(replacement)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert pin.is_file(), "the pin describes a different install and must be kept"
        assert pin.read_bytes() == replacement
        assert "kept the install record" in caplog.text, (
            "keeping the file silently would look exactly like a completed cleanup"
        )
        assert _state(store) == "uninstalled"
    finally:
        lifecycle.close()


async def test_a_pin_for_another_build_of_the_same_plugin_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The digest is part of the identity: same plugin, different build, kept."""
    lifecycle, _authority, store, pin = await _approved(tmp_path)
    try:
        replacement = _replacement(PLUGIN, "b" * 64)
        pin.write_bytes(replacement)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert pin.is_file()
        assert pin.read_bytes() == replacement
        assert "kept the install record" in caplog.text
        assert _state(store) == "uninstalled"
    finally:
        lifecycle.close()


async def test_a_pin_whose_identity_cannot_be_read_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unreadable is not "ours": a directory and invalid JSON are both kept."""
    lifecycle, _authority, _store, pin = await _approved(tmp_path)
    try:
        pin.unlink()
        pin.mkdir()

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await lifecycle.uninstall(PLUGIN)
        assert pin.is_dir(), "a pin that cannot be read must not be deleted"
        assert "identity cannot be read" in caplog.text

        pin.rmdir()
        pin.write_text('{"record_version": 1, "plugin_id": ', encoding="utf-8")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await lifecycle.uninstall(PLUGIN)
        assert pin.is_file(), "invalid JSON is not a pin this operation can claim"
        assert "identity cannot be read" in caplog.text
    finally:
        lifecycle.close()


async def test_a_pin_that_is_a_symbolic_link_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A link is refused even when it *resolves* to a matching pin.

    The identity check would pass — ``read_bytes`` follows the link — so this is
    the case that proves the ``is_symlink`` refusal is doing work rather than
    being covered by the digest comparison. Deleting the link would not delete
    what this install wrote, and a link can point outside the plugin directory.
    """
    lifecycle, _authority, _store, pin = await _approved(tmp_path)
    try:
        moved = tmp_path / "pin-elsewhere.json"
        pin.replace(moved)
        try:
            os.symlink(moved, pin)
        except OSError as exc:  # Windows without SeCreateSymbolicLinkPrivilege
            pytest.skip(f"this platform cannot create a symlink: {exc}")

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert pin.is_symlink(), "the link is not this record's file"
        assert moved.is_file(), "the target must not be reached through the link"
        assert "symbolic link" in caplog.text
    finally:
        lifecycle.close()


async def test_a_pin_that_is_already_gone_is_a_completed_cleanup(tmp_path: Path) -> None:
    """Nothing at the path is a finished removal, not a failure to report."""
    lifecycle, _authority, store, pin = await _approved(tmp_path)
    try:
        pin.unlink()

        record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert not pin.exists()
        assert _state(store) == "uninstalled"
    finally:
        lifecycle.close()


async def test_a_normal_uninstall_removes_the_pin_and_the_grants(tmp_path: Path) -> None:
    """The identity check must not make an honest uninstall stop working."""
    lifecycle, authority, store, pin = await _approved(tmp_path)
    try:
        assert pin.is_file()
        record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert not pin.exists(), "the pin this record describes must be removed"
        assert not any(authority.granted.values()), "the grants must be withdrawn"
        assert _state(store) == "uninstalled"
    finally:
        lifecycle.close()


async def test_a_retried_uninstall_does_not_delete_the_replacement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The retry is a cleanup: it finishes the removal and still refuses the stranger.

    The first attempt leaves the tombstone written and a foreign pin on disk. The
    second attempt must be *possible* (otherwise the state is unrecoverable by the
    verb that failed) and must not turn into an unconditional delete.
    """
    lifecycle, _authority, store, pin = await _approved(tmp_path)
    try:
        replacement = _replacement(f"{PLUGIN}-other", "c" * 64)
        pin.write_bytes(replacement)

        first = await lifecycle.uninstall(PLUGIN)
        assert first.state is InstallState.UNINSTALLED
        assert pin.is_file()

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            second = await lifecycle.uninstall(PLUGIN)

        assert second.state is InstallState.UNINSTALLED, (
            "re-running an uninstall that already recorded uninstalled must be a "
            "cleanup, not an illegal transition"
        )
        assert pin.is_file(), "the retry deleted a file the first run refused to touch"
        assert pin.read_bytes() == replacement
        assert "kept the install record" in caplog.text
        assert _state(store) == "uninstalled"
        assert second.approval is None and second.capabilities == (), (
            "a retry must not resurrect anything the tombstone cleared"
        )
    finally:
        lifecycle.close()
