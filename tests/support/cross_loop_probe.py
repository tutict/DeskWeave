"""A child process that awaits ``aclose()`` on a *second* event loop.

Phase 2.6.9a measured that the waiter never wakes when the in-flight operation
runs on another loop. That scenario cannot be driven from inside the test
process: a waiter that never returns would leave a thread, a loop and an
unreleased store lock behind for the rest of the session, and the next test would
inherit them. So the scenario lives here and the parent imposes the deadline.

The child is self-driving on purpose. Releasing the parked operation has to happen
*on loop A* (``call_soon_threadsafe``); a parent that reached in to do it would be
testing the parent's scheduling instead of the lifecycle's wake-up.

Protocol: exactly one ``VERDICT {...}`` line on stdout. Everything the library
logs goes to stderr, so a log line can never be mistaken for a verdict.

Exit codes: 0 = the child finished its own workflow (the verdict says whether
``aclose()`` returned), 3 = the deadline inside the child expired first, 1 = the
scenario could not be set up at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

#: ``tests/support`` is not a package the child can import by name unless its
#: parent directory is on the path; the fixtures live there, next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle
from deskweave_plugin_wasm.locking import held_keys
from support import wasm_plugins as fixtures
from support.inflight import GatedAuthority

VALID = "reference.metadata.validate"
PLUGIN = "cross-loop"
#: Ceilings, not waits: reaching one is the datum the verdict reports.
SETUP_S = 30.0
RETURN_S = 20.0
#: Exit code for "the deadline expired with ``aclose()`` still pending".
RT_DEADLINE = 3


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


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write("VERDICT " + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def main(directory: Path) -> int:
    authority = GatedAuthority()
    store_path = directory / "approvals.json"
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None),
        permissions=authority,
        store=JsonApprovalStore(store_path),
    )
    key = os.path.normcase(str(store_path.resolve()))

    loop_a = asyncio.new_event_loop()
    loop_b: asyncio.AbstractEventLoop | None = None
    verdict: dict[str, object] = {
        "aclose_returned": False,
        "aclose_value": None,
        "aclose_error": None,
        "host_close_calls": 0,
        "op_completed": False,
        "store_lock_released": False,
        "elapsed_s": None,
    }

    host_close_calls: list[int] = []
    real_host_close = lifecycle.host.aclose

    async def counting_host_close() -> int:
        host_close_calls.append(1)
        return await real_host_close()

    lifecycle.host.aclose = counting_host_close  # type: ignore[method-assign]

    async def _prepare() -> None:
        manifest = _manifest(directory)
        await lifecycle.install(manifest)
        await lifecycle.approve(PLUGIN, [VALID])

    def run_operation() -> None:
        asyncio.set_event_loop(loop_a)
        try:
            loop_a.run_until_complete(_prepare())
            # Parks inside permissions.revoke until the gate is released.
            loop_a.run_until_complete(lifecycle.revoke(PLUGIN))
            verdict["op_completed"] = True
        finally:
            loop_a.close()

    def run_aclose() -> None:
        nonlocal loop_b
        loop_b = asyncio.new_event_loop()
        asyncio.set_event_loop(loop_b)
        started = time.monotonic()
        try:
            verdict["aclose_value"] = loop_b.run_until_complete(lifecycle.aclose())
            verdict["aclose_returned"] = True
        except BaseException as exc:
            verdict["aclose_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            verdict["elapsed_s"] = round(time.monotonic() - started, 3)
            loop_b.close()

    worker = threading.Thread(target=run_operation, name="loopA", daemon=True)
    worker.start()

    # Wait for the *revocation* to be parked, not merely for the counter to be
    # non-zero: install() and approve() also count themselves in flight, and a
    # probe that started aclose() during one of those would measure the wrong
    # thing (and could close the lifecycle before revoke() even began).
    deadline = time.monotonic() + SETUP_S
    while time.monotonic() < deadline and not authority.reached.is_set():
        time.sleep(0.02)
    if not authority.reached.is_set():
        _emit({**verdict, "stage": "setup", "problem": "the revocation never parked"})
        return 1

    closer = threading.Thread(target=run_aclose, name="loopB", daemon=True)
    closer.start()

    # Let loop B reach its wait, then release the operation *on loop A*.
    time.sleep(0.5)
    loop_a.call_soon_threadsafe(authority.release)

    worker.join(timeout=RETURN_S)
    closer.join(timeout=RETURN_S)

    verdict["host_close_calls"] = len(host_close_calls)
    verdict["store_lock_released"] = key not in held_keys()
    verdict["stage"] = "returned" if verdict["aclose_returned"] else "deadline"
    _emit(verdict)
    return 0 if verdict["aclose_returned"] else RT_DEADLINE


if __name__ == "__main__":
    target = Path(sys.argv[1])
    raise SystemExit(main(target))
