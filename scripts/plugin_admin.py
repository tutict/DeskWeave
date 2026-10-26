"""Read-only inspection for installed WASM plugins.

Deliberately read-only. ``approve``, ``revoke`` and ``uninstall`` are *decisions*,
and a decision needs a surface a person is actually looking at — the desktop
confirmation UI, wired to ``PluginLifecycle``. Shipping them as shell verbs would
create the exact shortcut this phase is supposed to close: a command that grants
capabilities with no UI, no audit event and no record of who approved what.

What this tool can do is answer "what is installed, what does it ask for, what was
approved, and does the file still match":

    uv run python scripts/plugin_admin.py list examples/wasm-plugins
    uv run python scripts/plugin_admin.py inspect reference-metadata \\
        --plugins-dir examples/wasm-plugins --store .deskweave/approvals.json

Exit codes: 0 = inspected successfully, 2 = the plugin could not be found, 3 = the
on-disk state disagrees with the pin (a drift, not a crash).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deskweave_plugin_wasm.integrity import (
    INSTALL_RECORD_NAME,
    InstallRecord,
    sha256_file,
)
from deskweave_plugin_wasm.lifecycle import LifecycleRecord, discover
from deskweave_plugin_wasm.manifest import WasmPluginManifest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_store(path: Path | None) -> dict[str, LifecycleRecord]:
    """Read approval state if a store was named. Absent means *unknown*, not empty."""
    if path is None or not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    plugins = raw.get("plugins", {}) if isinstance(raw, dict) else {}
    return {plugin_id: LifecycleRecord.from_dict(entry) for plugin_id, entry in plugins.items()}


def _each_plugin_dir(directory: Path) -> list[Path]:
    """The directory itself plus its immediate children, so ``examples/wasm-plugins`` works."""
    if not directory.is_dir():
        return []
    return [directory, *sorted(item for item in directory.iterdir() if item.is_dir())]


def _fmt(value: object, width: int = 28) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def list_plugins(directory: Path, store: Path | None) -> int:
    found = [item for candidate in _each_plugin_dir(directory) for item in discover(candidate)]
    if not found:
        print(f"no plugin manifests found in {directory}")
        return 2
    records = _load_store(store)
    print(f"{'plugin_id':<26} {'version':<10} {'abi':<4} {'pinned':<7} {'state':<12} requested")
    print("-" * 96)
    for item in found:
        record = records.get(item.plugin_id)
        state = record.state.value if record else ("unknown" if store else "-")
        print(
            f"{_fmt(item.plugin_id, 26):<26} {_fmt(item.version, 10):<10} "
            f"{item.interface_version:<4} {item.pinned!s:<7} {state:<12} "
            f"{','.join(item.requested) or '(none)'}"
        )
    return 0


def inspect_plugin(plugin_id: str, directory: Path, store: Path | None) -> int:
    matches = [
        (candidate, item)
        for candidate in _each_plugin_dir(directory)
        for item in discover(candidate)
        if item.plugin_id == plugin_id
    ]
    if not matches:
        print(f"plugin {plugin_id!r} not found in {directory}", file=sys.stderr)
        return 2
    plugin_dir, entry = matches[0]

    manifest_path = plugin_dir / entry.manifest_name
    manifest = WasmPluginManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    module_path = manifest_path.parent / entry.module_name
    record = _load_store(store).get(plugin_id)

    actual = None
    if module_path.is_file():
        actual, size = sha256_file(module_path)
    else:
        size = 0

    pin_state = "no install record"
    pin_digest = None
    pin_path = manifest_path.parent / INSTALL_RECORD_NAME
    if pin_path.is_file():
        # Read the record and compare, rather than calling `verify_install`: the
        # verifier refuses on drift (by design), and a report that can only say
        # "invalid" cannot tell an operator *what* the pin claims.
        try:
            pin = InstallRecord.from_dict(
                json.loads(pin_path.read_text(encoding="utf-8")), source=pin_path.name
            )
            pin_digest = pin.wasm_sha256
            pin_state = "matching" if actual == pin.wasm_sha256 else "MODIFIED SINCE INSTALL"
        except Exception as exc:  # reported, not swallowed
            pin_state = f"INVALID RECORD ({getattr(exc, 'code', 'error')})"

    drift = "unknown (no approval store given)"
    if record is not None:
        if record.approval is None:
            drift = "no approval"
        else:
            added = sorted(set(manifest.declared_capabilities) - set(record.approval.requested))
            stale = (
                record.approval.wasm_sha256 != actual
                or record.approval.version != manifest.version
                or bool(added)
            )
            drift = (
                f"STALE (added: {', '.join(added)})"
                if added
                else ("stale" if stale else "covered by approval")
            )

    print(f"plugin_id        {plugin_id}")
    print(f"version          {manifest.version}")
    print(f"abi version      {manifest.interface_version}")
    print(f"manifest         {entry.manifest_name}")
    print(f"module           {entry.module_name} ({size} bytes)")
    print(f"sha256 (disk)    {actual or '(module missing)'}")
    print(f"sha256 (pin)     {pin_digest or '(none)'}")
    print(f"pin state        {pin_state}")
    print(f"requested        {', '.join(entry.requested) or '(none)'}")
    if record is None:
        print("approved         unknown (no approval store given)")
        print("state            unknown")
    else:
        approved = ", ".join(sorted(record.approval.capabilities)) if record.approval else "(none)"
        print(f"approved         {approved}")
        print(f"state            {record.state.value}")
        print(f"last event       {record.last_event.value if record.last_event else '(none)'}")
        if record.last_detail:
            print(f"last detail      {json.dumps(record.last_detail, sort_keys=True)}")
    print(f"approval check   {drift}")

    drifted = pin_state.startswith(("MODIFIED", "INVALID")) or drift.startswith("STALE")
    return 3 if drifted else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list plugin manifests in a directory")
    listing.add_argument("plugins_dir", type=Path)
    listing.add_argument("--store", type=Path, default=None, help="approval store to read")

    detail = sub.add_parser("inspect", help="show one plugin in detail")
    detail.add_argument("plugin_id")
    detail.add_argument("--plugins-dir", type=Path, default=REPO_ROOT / "examples" / "wasm-plugins")
    detail.add_argument("--store", type=Path, default=None, help="approval store to read")

    args = parser.parse_args(argv)
    if args.command == "list":
        return list_plugins(args.plugins_dir, args.store)
    return inspect_plugin(args.plugin_id, args.plugins_dir, args.store)


if __name__ == "__main__":
    raise SystemExit(main())
