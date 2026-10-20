"""The state file tells tooling where the runtime is — never what its token is.

Two behaviours are pinned here:

* a token written by an older version is removed on the next start (and the
  removal is logged), because the file is readable by anything running as the
  same user;
* a corrupt state file must not prevent the runtime from starting.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOKEN = "state-file-token"


def _start(data_dir: Path, token: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "deskweave_runtime.service",
            "--announce",
            "--port",
            "0",
            "--data-dir",
            str(data_dir),
            "--token",
            token,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )


async def _ready(process: subprocess.Popen[str]) -> int:
    assert process.stdout is not None
    line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=30)
    handshake = json.loads(line)
    assert handshake["event"] == "ready"
    return int(handshake["port"])


async def _stop(process: subprocess.Popen[str], port: int) -> str:
    """Ask the service to exit and return everything it wrote to stderr."""

    def _shutdown_request() -> None:
        import urllib.request

        request = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", method="POST")
        request.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

    await asyncio.to_thread(_shutdown_request)
    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=30)
    assert process.stderr is not None
    return await asyncio.to_thread(process.stderr.read)


@pytest.mark.parametrize("legacy", ["token", "corrupt"], ids=["legacy-token", "corrupt-file"])
async def test_state_file_is_rewritten_without_the_token(tmp_path: Path, legacy: str) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = data_dir / "runtime.json"
    if legacy == "token":
        state_path.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 1,
                    "url": "http://127.0.0.1:1",
                    "token": "old-plaintext",
                }
            ),
            encoding="utf-8",
        )
    else:
        state_path.write_text("{not json", encoding="utf-8")

    process = _start(data_dir, TOKEN)
    try:
        port = await _ready(process)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert "token" not in state
        # `pid` is the interpreter's, which differs from the launcher's pid when
        # the venv `python.exe` re-execs (Windows).
        assert isinstance(state["pid"], int) and state["pid"] > 0
        assert state["port"] == port
        assert "old-plaintext" not in state_path.read_text(encoding="utf-8")

        stderr = await _stop(process, port)
        if legacy == "token":
            assert "removed a legacy plaintext token" in stderr
        else:
            assert "ignoring an unreadable" in stderr
    finally:
        if process.poll() is None:
            process.kill()


async def test_the_token_is_never_logged_and_never_persisted(tmp_path: Path) -> None:
    """Nothing the runtime writes may contain the token: not the log, not the data dir.

    The log is the easy place to leak it (a careless "request headers: …" line), and
    the data directory outlives the process, so it is checked byte for byte.
    """
    import urllib.error
    import urllib.request

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    process = _start(data_dir, TOKEN)
    try:
        port = await _ready(process)

        def _hit(token: str | None) -> int:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            if token is not None:
                request.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return int(response.status)
            except urllib.error.HTTPError as exc:
                return int(exc.code)

        assert await asyncio.to_thread(_hit, TOKEN) == 200
        assert await asyncio.to_thread(_hit, None) == 401
        assert await asyncio.to_thread(_hit, "wrong-token") == 401

        stderr = await _stop(process, port)
    finally:
        if process.poll() is None:
            process.kill()

    stdout = process.stdout.read() if process.stdout else ""
    assert TOKEN not in stderr
    assert TOKEN not in stdout

    for path in data_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), f"the token leaked into {path.name}"
