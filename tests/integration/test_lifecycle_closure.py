"""Phase 2.6.6 P1: a closed lifecycle is not an authority any more.

``close()`` released the store lock but left every entry point working: a
lifecycle could keep writing the store after another process had taken it over
(Phase 2.6.5 audit, probe B3b). These tests assert both halves — the refusal and
the absence of side effects — because an entry point that raises *after* writing
would pass a "raises" test and still be the bug.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import (
    PluginLifecycleError,
    PluginNotApprovedError,
)
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import (
    JsonApprovalStore,
    PluginLifecycle,
)
from support import wasm_plugins as fixtures
from support.store_lock import Holder, probe

VALID = "reference.metadata.validate"
PLUGIN = "closed"


class RecordingPermissions:
    def __init__(self) -> None:
        self.revoked = 0

    async def list_effective_grants(self) -> list[dict[str, Any]]:
        return []

    async def grant(self, request: Any, **kwargs: Any) -> str:
        return "granted"

    async def revoke(self, principal: str, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        self.revoked += 1
        return ("closed.grant",)


def _manifest(tmp_path: Path) -> Path:
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        tmp_path, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
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


def _lifecycle(store: Any) -> PluginLifecycle:
    return PluginLifecycle(
        registry=default_registry(None, None),
        permissions=RecordingPermissions(),
        store=store,
    )


def _bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


@pytest.fixture
def approved(tmp_path: Path) -> Any:
    lifecycle = _lifecycle(JsonApprovalStore(tmp_path / "approvals.json"))
    manifest = _manifest(tmp_path)

    async def build() -> tuple[PluginLifecycle, Path, Path]:
        await lifecycle.install(manifest)
        await lifecycle.approve(PLUGIN, [VALID])
        return lifecycle, manifest, tmp_path / "approvals.json"

    yield build
    lifecycle.close()


async def test_every_writing_entry_point_refuses_after_close(approved: Any) -> None:
    """Six entry points, one refusal, and not one byte written."""
    lifecycle, manifest, store = await approved()
    before = _bytes(store)
    pin = manifest.parent / "deskweave.install.json"
    pin_before = _bytes(pin)

    lifecycle.close()
    assert lifecycle.closed is True

    with pytest.raises(PluginLifecycleError) as caught:
        await lifecycle.install(manifest)
    assert caught.value.details["lifecycle"] == "closed"
    with pytest.raises(PluginLifecycleError):
        await lifecycle.approve(PLUGIN, [VALID])
    with pytest.raises(PluginLifecycleError):
        await lifecycle.revoke(PLUGIN)
    with pytest.raises(PluginLifecycleError):
        await lifecycle.uninstall(PLUGIN)
    with pytest.raises(PluginLifecycleError):
        await lifecycle.activate(manifest)
    with pytest.raises(PluginLifecycleError):
        await lifecycle.discover(manifest.parent)

    assert _bytes(store) == before, "a closed lifecycle must not have written the store"
    assert _bytes(pin) == pin_before, "a closed lifecycle must not have re-pinned anything"
    state = json.loads(store.read_text(encoding="utf-8"))["plugins"][PLUGIN]["state"]
    assert state == "approved"


async def test_a_closed_lifecycle_cannot_authorise_a_load(approved: Any) -> None:
    """The indirect path: no guard, no dispatch, no instantiation from a closed host."""
    lifecycle, manifest, _store = await approved()
    lifecycle.close()

    with pytest.raises(PluginLifecycleError) as caught:
        lifecycle.host.load(manifest.with_suffix(".wasm"), manifest, approval={})
    assert caught.value.details["lifecycle"] == "closed"

    with pytest.raises(PluginNotApprovedError) as refused:
        await lifecycle._dispatch_guard(PLUGIN, VALID)
    assert refused.value.details["state"] == "lifecycle_closed"


async def test_close_is_idempotent_and_keeps_other_holders(approved: Any, tmp_path: Path) -> None:
    """Closing one instance releases one reference; the second still governs."""
    first, _manifest_path, store = await approved()
    second = _lifecycle(JsonApprovalStore(store))

    first.close()
    first.close()
    assert first.closed is True
    assert probe(store)["error"] == "locked", "the second lifecycle still holds the store"

    second.close()
    second.close()
    verdict = probe(store)
    assert verdict.get("ok") is True, "the last close must actually release the lock"


async def test_a_closed_lifecycle_cannot_write_while_another_process_governs(
    approved: Any, tmp_path: Path
) -> None:
    """The one-writer rule, on the path that used to lose it."""
    lifecycle, _manifest_path, store = await approved()
    lifecycle.close()

    with Holder(store, tmp_path / "ready.json") as holder:
        holder.wait_ready()
        before = _bytes(store)
        with pytest.raises(PluginLifecycleError):
            await lifecycle.approve(PLUGIN, [VALID])
        assert _bytes(store) == before
