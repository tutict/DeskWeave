"""A separate process for the cross-process store-lock tests.

Spawned by ``tests/integration/test_store_lock.py`` with ``sys.executable``. It
exists so that "two processes" means two *processes*: an OS file lock is only
meaningful across them, and two objects in one interpreter share one lock by
design (reference counted), which would prove nothing.

Two modes:

``hold``
    Take the lock and stay alive until told what to do, reporting through a
    ready file. ``release`` drops the lock normally; ``crash`` calls
    ``os._exit`` so no Python cleanup runs at all — the process dies the way a
    killed one would, and only the operating system can release the lock.

``probe``
    Try to build a full ``PluginLifecycle`` over a store and report the verdict
    as one JSON line. ``--cwd`` changes directory first, so a relative ``--store``
    names the same file from somewhere else.

Result lines go to stdout; anything the library logs goes to stderr, which the
parent captures separately so a log line can never be mistaken for a verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _probe(store: str) -> int:
    from deskweave_plugin_wasm.errors import PluginError, PluginStoreLockedError
    from deskweave_plugin_wasm.host import default_registry
    from deskweave_plugin_wasm.lifecycle import JsonApprovalStore, PluginLifecycle

    try:
        lifecycle = PluginLifecycle(
            registry=default_registry(None, None),
            permissions=None,
            store=JsonApprovalStore(store),
        )
    except PluginStoreLockedError as exc:
        _emit({"event": "probe", "ok": False, "error": "locked", "details": exc.details})
        return 0
    except PluginError as exc:
        _emit(
            {
                "event": "probe",
                "ok": False,
                "error": type(exc).__name__,
                "message": exc.message,
                "details": exc.details,
            }
        )
        return 0

    from deskweave_plugin_wasm.locking import held_keys

    # Reaching here means this process believes it governs the store. Report
    # enough for the parent to check *which* store, then let go of it.
    _emit(
        {
            "event": "probe",
            "ok": True,
            "store": str(lifecycle._store.path),  # the resolved path, not the argument
            "held": sorted(held_keys()),
        }
    )
    lifecycle.close()
    return 0


def _hold(store: str, ready: str) -> int:
    from deskweave_plugin_wasm.locking import acquire, release

    lock = acquire(store)
    Path(ready).write_text(
        json.dumps({"pid": os.getpid(), "lock_path": str(lock.lock_path)}),
        encoding="utf-8",
    )
    command = sys.stdin.readline().strip()
    if command == "crash":
        # No finally, no atexit, no finalizer: this is the abnormal-exit case.
        os._exit(9)
    release(lock)
    _emit({"event": "released", "lock_path": str(lock.lock_path)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lock_probe")
    parser.add_argument("mode", choices=("hold", "probe"))
    parser.add_argument("--store", required=True)
    parser.add_argument("--cwd")
    parser.add_argument("--ready")
    args = parser.parse_args(argv)

    if args.cwd:
        os.chdir(args.cwd)

    if args.mode == "probe":
        return _probe(args.store)
    if not args.ready:
        parser.error("--ready is required for hold")
    return _hold(args.store, args.ready)


if __name__ == "__main__":
    sys.exit(main())
