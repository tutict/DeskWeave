"""A second *process* for the store-lock tests.

``tests/support/lock_probe.py`` is the child program; this is the parent's handle
on it. Two tests in this suite need one (the 2.6.4 lock tests write their own
``Holder``; newer files use this), and a copy of "start it, wait for the ready
file, kill it" in each of them is the kind of duplication that hides a missing
``wait``.

No sleeps for synchronisation: the child writes its ready file *after* it holds
the lock, and the parent waits for that file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parent / "lock_probe.py"
#: A real interpreter with real imports; a ceiling, not a wait.
PROBE_TIMEOUT = 120


class Holder:
    """A child process holding the store lock until told otherwise."""

    def __init__(self, store: str | Path, ready: Path, *, cwd: Path | None = None) -> None:
        self.ready = ready
        command = [sys.executable, str(PROBE), "hold", "--store", str(store), "--ready", str(ready)]
        if cwd is not None:
            command += ["--cwd", str(cwd)]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_ready(self) -> dict[str, Any]:
        deadline = time.monotonic() + PROBE_TIMEOUT
        while time.monotonic() < deadline:
            if self.ready.is_file():
                return json.loads(self.ready.read_text(encoding="utf-8"))
            if self.process.poll() is not None:
                _out, err = self.process.communicate()
                raise AssertionError(f"the holder exited without taking the lock: {err}")
            time.sleep(0.02)
        raise AssertionError("the holder never reported holding the lock")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate(timeout=PROBE_TIMEOUT)

    def __enter__(self) -> Holder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def probe(store: str | Path, *, cwd: Path | None = None) -> dict[str, Any]:
    """Try to build a lifecycle over ``store`` in a new process; report the verdict.

    ``{"ok": True, ...}`` means the child believes it governs the store;
    ``{"ok": False, "error": "locked"}`` means this process still holds it.
    """
    command = [sys.executable, str(PROBE), "probe", "--store", str(store)]
    if cwd is not None:
        command += ["--cwd", str(cwd)]
    done = subprocess.run(
        command, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
    )
    for line in done.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"the probe said nothing usable: {done.stdout!r} {done.stderr[-300:]}")
