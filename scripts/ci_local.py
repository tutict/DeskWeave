"""Run the GitHub Actions workflow locally, command for command.

CI is only trustworthy if the same commands are reproducible outside GitHub. This
script mirrors `.github/workflows/ci.yml` (minus the Linux-only setup steps) so a
failure can be diagnosed on the development machine before it ever reaches the
runner.

Usage::

    uv run python scripts/ci_local.py            # every job
    uv run python scripts/ci_local.py python     # one job
    uv run python scripts/ci_local.py --list

Exit code is 0 only when every selected command succeeded.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = REPO_ROOT / "apps" / "desktop" / "src-tauri"


@dataclass(frozen=True)
class Step:
    name: str
    argv: list[str]
    cwd: Path


def _pnpm() -> list[str]:
    """Resolve pnpm, including the Windows ``.CMD`` shim case.

    ``shutil.which("pnpm")`` returns ``pnpm.CMD`` on Windows, but ``CreateProcess``
    cannot start a batch file, so a direct spawn fails with "not installed" on a
    machine where pnpm works perfectly in a shell. Batch shims go through the
    command interpreter; everything else is executed as-is.
    """
    found = shutil.which("pnpm")
    if found is None:
        corepack = shutil.which("corepack")
        if corepack is None:
            return ["pnpm"]  # run() reports this as a readable 127
        found, extra = corepack, ["pnpm"]
    else:
        extra = []
    if sys.platform == "win32" and found.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", found, *extra]
    return [found, *extra]


def jobs() -> dict[str, list[Step]]:
    pnpm = _pnpm()
    return {
        "python": [
            Step(
                "uv sync --all-packages --locked",
                ["uv", "sync", "--all-packages", "--locked"],
                REPO_ROOT,
            ),
            Step("ruff check", ["uv", "run", "ruff", "check", "."], REPO_ROOT),
            Step("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."], REPO_ROOT),
            Step("pytest", ["uv", "run", "pytest"], REPO_ROOT),
        ],
        "frontend": [
            Step(
                "pnpm install --frozen-lockfile", [*pnpm, "install", "--frozen-lockfile"], REPO_ROOT
            ),
            Step("pnpm build", [*pnpm, "build"], REPO_ROOT),
            Step("pnpm test", [*pnpm, "test"], REPO_ROOT),
            Step("pnpm lint", [*pnpm, "lint"], REPO_ROOT),
            Step("pnpm format:check", [*pnpm, "format:check"], REPO_ROOT),
        ],
        "rust": [
            Step(
                "cargo check --all-targets --locked",
                ["cargo", "check", "--all-targets", "--locked"],
                TAURI_DIR,
            ),
            Step("cargo fmt --check", ["cargo", "fmt", "--check"], TAURI_DIR),
            Step("cargo test --lib --locked", ["cargo", "test", "--lib", "--locked"], TAURI_DIR),
        ],
    }


def run(job: str, steps: list[Step]) -> list[tuple[str, str, float]]:
    results: list[tuple[str, str, float]] = []
    for step in steps:
        print(f"\n=== [{job}] {step.name}", flush=True)
        started = time.monotonic()
        try:
            code = subprocess.run(step.argv, cwd=step.cwd, check=False).returncode
        except FileNotFoundError:
            print(f"!!! {step.argv[0]!r} is not installed or not on PATH", flush=True)
            code = 127
        elapsed = time.monotonic() - started
        outcome = "PASS" if code == 0 else f"FAIL ({code})"
        results.append((step.name, outcome, elapsed))
        print(f"--- [{job}] {step.name}: {outcome} in {elapsed:.1f}s", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    available = jobs()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("job", nargs="?", choices=[*available, "all"], default="all")
    parser.add_argument("--list", action="store_true", help="print the mirrored commands and exit")
    args = parser.parse_args(argv)

    if args.list:
        for job, steps in available.items():
            print(f"{job}:")
            for step in steps:
                print(f"  - {step.name}  (cwd={step.cwd.relative_to(REPO_ROOT) or '.'})")
        return 0

    selected = available if args.job == "all" else {args.job: available[args.job]}
    summary: list[tuple[str, str, float]] = []
    for job, steps in selected.items():
        summary.extend(run(job, steps))

    print("\n=== summary")
    width = max(len(name) for name, _, _ in summary)
    for name, outcome, elapsed in summary:
        print(f"{name.ljust(width)}  {outcome:>10}  {elapsed:6.1f}s")
    failed = [name for name, outcome, _ in summary if outcome != "PASS"]
    if failed:
        print(f"\n{len(failed)} step(s) failed: {', '.join(failed)}")
        return 1
    print("\nall mirrored CI steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
