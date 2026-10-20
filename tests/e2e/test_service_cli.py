"""End-to-end test: launch the runtime service CLI and talk to it over HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = "cli-e2e-token"


def _get(url: str, token: str, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def test_service_cli_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "e2e-data"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
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
            TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert process.stdout is not None
        handshake_line = await asyncio.wait_for(
            asyncio.to_thread(process.stdout.readline), timeout=30
        )
        handshake = json.loads(handshake_line)
        assert handshake["event"] == "ready"
        port = handshake["port"]

        state = json.loads((data_dir / "runtime.json").read_text(encoding="utf-8"))
        # The token must not be on disk: it is passed in, not persisted.
        assert "token" not in state, state
        assert state["port"] == port
        # On Windows the venv `python.exe` is a launcher that re-execs the real
        # interpreter, so the recorded pid is the interpreter's, not the launcher's.
        assert isinstance(state["pid"], int) and state["pid"] > 0

        status, payload = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/health", TOKEN)
        assert status == 200
        assert payload["state"] == "ok"

        # A wrong token still fails even though the port is discoverable.
        status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/health", "nope")
        assert status == 401

        status, payload = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{port}/shutdown", TOKEN, "POST"
        )
        assert status == 200

        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=30)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
