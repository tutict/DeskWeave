"""Build the example WASM plugin and place the artifact next to its manifest.

The Python test suite runs against the committed ``.wasm`` so that tests stay
hermetic and CI does not need a Rust toolchain (or a ``wasm32-wasip1`` target) to
verify the host. This script is how that artifact is (re)produced.

    uv run python scripts/build_wasm_plugin.py

Requires ``rustup target add wasm32-wasip1`` once.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "examples" / "wasm-plugins" / "reference-metadata"
TARGET = "wasm32-wasip1"
CRATE = "deskweave_plugin_reference_metadata"
ARTIFACT_NAME = "reference_metadata.wasm"


def main() -> int:
    built = EXAMPLE_DIR / "target" / TARGET / "release" / f"{CRATE}.wasm"
    print(f"building {EXAMPLE_DIR.name} for {TARGET} ...")
    result = subprocess.run(
        ["cargo", "build", "--release", "--target", TARGET, "--locked"],
        cwd=EXAMPLE_DIR,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        # --locked fails on a missing Cargo.lock; retry once without it so a cold
        # checkout can produce the lockfile.
        result = subprocess.run(
            ["cargo", "build", "--release", "--target", TARGET],
            cwd=EXAMPLE_DIR,
            text=True,
            capture_output=True,
        )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-4000:], file=sys.stderr)
        return result.returncode

    if not built.is_file():
        print(f"expected artifact not found: {built}", file=sys.stderr)
        return 1

    destination = EXAMPLE_DIR / ARTIFACT_NAME
    shutil.copyfile(built, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    size = destination.stat().st_size
    print(f"wrote {destination.relative_to(REPO_ROOT)} ({size} bytes)")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
