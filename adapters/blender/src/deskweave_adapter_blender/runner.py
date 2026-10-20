"""Locate and drive a Blender executable.

The adapter talks to Blender the only way that is safe from a long-lived service:
a *headless, controlled* subprocess running a fixed script. No ``bpy`` import into
the runtime process (there is none in a normal Python install), no dynamic code,
no shell.

Everything that can fail is turned into an actionable
:class:`~deskweave_contracts.AdapterError` subclass so the UI and the agent can
tell "Blender is missing" from "the script failed".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from deskweave_contracts import (
    AdapterConnectionError,
    AdapterExecutionError,
    AdapterTimeoutError,
    UnsafeResourceError,
)

RESULT_PREFIX = "DESKWEAVE_RESULT "
SCENE_PREFIX = "DESKWEAVE_SCENE "
ERROR_PREFIX = "DESKWEAVE_ERROR "

#: Blender's Python prints its own banners; keep only the tail for error messages.
_STDERR_TAIL = 2000
_VERSION_RE = re.compile(r"Blender\s+(?P<version>\d+\.\d+(?:\.\d+)?)")

#: Minimum supported Blender. Below 3.0 the material/plane API differs enough that
#: the script would silently produce the wrong scene, so refuse instead.
MIN_VERSION = (3, 0)


@dataclass(frozen=True)
class BlenderInfo:
    executable: Path
    version: str

    @property
    def version_tuple(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.version.split("."))


def _candidate_paths(env: dict[str, str]) -> list[Path]:
    candidates: list[Path] = []
    explicit = env.get("DESKWEAVE_BLENDER")
    if explicit:
        candidates.append(Path(explicit))

    if sys.platform.startswith("win"):
        for root in (
            Path(env.get("PROGRAMFILES", r"C:\Program Files")),
            Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
        ):
            foundation = root / "Blender Foundation"
            if foundation.is_dir():
                # Newest install first: "Blender 5.2" sorts after "Blender 4.4".
                for install in sorted(foundation.iterdir(), reverse=True):
                    executable = install / "blender.exe"
                    if executable.is_file():
                        candidates.append(executable)
            candidate = root / "Blender" / "blender.exe"
            if candidate.is_file():
                candidates.append(candidate)
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Blender.app/Contents/MacOS/Blender"))
    else:
        candidates.extend([Path("/usr/bin/blender"), Path("/usr/local/bin/blender")])

    on_path = shutil.which("blender")
    if on_path:
        candidates.append(Path(on_path))
    return candidates


def find_blender(env: dict[str, str] | None = None) -> Path:
    """Return the first usable Blender executable, or raise with instructions."""
    environment = dict(os.environ if env is None else env)
    seen: list[str] = []
    for candidate in _candidate_paths(environment):
        if candidate in seen:
            continue
        seen.append(str(candidate))
        if candidate.is_file():
            return candidate
    raise AdapterConnectionError(
        "no Blender executable found; install Blender or set DESKWEAVE_BLENDER "
        "to the full path of the blender binary",
        details={
            "adapter_id": "blender",
            "checked": seen,
            "hint": "https://www.blender.org/download/",
        },
    )


def _script_path(name: str) -> Path:
    """Return the on-disk path of a bundled script."""
    resource = resources.files("deskweave_adapter_blender").joinpath("scripts", name)
    path = Path(str(resource))
    if not path.is_file():
        raise AdapterExecutionError(
            f"bundled Blender script {name!r} is missing from the installation",
            details={"path": str(path)},
        )
    return path


class BlenderRunner:
    """Invokes Blender headless and parses its structured output."""

    def __init__(
        self,
        executable: Path | None = None,
        *,
        timeout: float = 150.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._explicit = executable
        self._timeout = timeout
        self._env_override = env
        self._info: BlenderInfo | None = None

    # -- discovery --------------------------------------------------------
    def info(self) -> BlenderInfo:
        """Return the executable and version, probing once."""
        if self._info is not None:
            return self._info
        executable = self._explicit or find_blender(self._env_override)
        if not executable.is_file():
            raise AdapterConnectionError(
                f"Blender executable does not exist: {executable}",
                details={"executable": str(executable)},
            )
        try:
            completed = self._spawn([str(executable), "--version"], timeout=60)
        except OSError as exc:
            raise AdapterConnectionError(
                f"could not run {executable}: {exc}", details={"executable": str(executable)}
            ) from exc
        output = f"{completed.stdout}\n{completed.stderr}"
        match = _VERSION_RE.search(output)
        if completed.returncode != 0 or match is None:
            raise AdapterConnectionError(
                f"{executable} did not report a Blender version",
                details={"returncode": completed.returncode, "output": _tail(output)},
            )
        version = match.group("version")
        info = BlenderInfo(executable=executable, version=version)
        if info.version_tuple < MIN_VERSION:
            raise AdapterConnectionError(
                f"Blender {version} is too old; {'.'.join(map(str, MIN_VERSION))}+ is required",
                details={"executable": str(executable), "version": version},
            )
        self._info = info
        return info

    # -- execution --------------------------------------------------------
    def run_script(self, script: str, *, argument: str, value: str) -> dict[str, Any]:
        info = self.info()
        command = [
            str(info.executable),
            "--background",
            "--factory-startup",
            "--python-exit-code",
            "3",
            "--python",
            str(_script_path(script)),
            "--",
            argument,
            value,
        ]
        try:
            completed = self._spawn(command, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise AdapterTimeoutError(
                f"Blender did not finish {script!r} within {self._timeout:.0f}s",
                details={"script": script, "timeout_seconds": self._timeout},
            ) from exc
        except OSError as exc:
            raise AdapterConnectionError(
                f"could not launch Blender: {exc}",
                details={"executable": str(info.executable)},
            ) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        marker = ERROR_PREFIX.strip()
        if completed.returncode != 0:
            detail = _extract(stderr, ERROR_PREFIX) or _tail(stderr) or _tail(stdout)
            raise AdapterExecutionError(
                f"Blender script {script!r} failed (exit {completed.returncode}): {detail}",
                details={"script": script, "returncode": completed.returncode},
            )
        payload = _extract(stdout, RESULT_PREFIX) or _extract(stdout, SCENE_PREFIX)
        if payload is None:
            raise AdapterExecutionError(
                f"Blender script {script!r} produced no result; the scene state is unknown",
                details={
                    "script": script,
                    "output": _tail(stdout) or _tail(stderr),
                    "marker": marker,
                },
            )
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AdapterExecutionError(
                f"Blender script {script!r} returned malformed JSON: {exc}",
                details={"script": script, "output": _tail(payload)},
            ) from exc

    def _spawn(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(command, **kwargs)


def _extract(output: str, prefix: str) -> str | None:
    """Return the payload of the last ``prefix`` line, or ``None``."""
    found = None
    for line in output.splitlines():
        index = line.find(prefix)
        if index >= 0:
            found = line[index + len(prefix) :].strip()
    return found


def _tail(text: str, limit: int = _STDERR_TAIL) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def ensure_output_path(root: Path, task_id: str | None, name: str) -> Path:
    """Build a blend output path inside ``root``.

    The path is derived from the task and name — never from caller-supplied
    paths — and re-checked for containment so a crafted name cannot escape.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "reference"
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", task_id or "manual").strip("-") or "manual"
    target = (root / f"{stem}-{suffix}.blend").resolve()
    if target.parent != root:
        raise UnsafeResourceError(
            "refusing to write the blend file outside the output directory",
            details={"target": str(target)},
        )
    return target


__all__ = [
    "MIN_VERSION",
    "RESULT_PREFIX",
    "SCENE_PREFIX",
    "BlenderInfo",
    "BlenderRunner",
    "ensure_output_path",
    "find_blender",
]
