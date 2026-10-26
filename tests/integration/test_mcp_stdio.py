"""End-to-end MCP test over a real stdio subprocess.

The in-memory tests (`test_mcp_protocol.py`) prove the request/response layer.
This file proves the *deployment* shape that Codex will actually use:

1. ``python -m deskweave_mcp_gateway`` starts as a child process;
2. a real client can initialize and call a tool over pipes;
3. stdout carries protocol frames only — logs go to stderr.

Assertion 3 is checked twice: implicitly (a polluted stdout would break the
client handshake) and explicitly (nothing is written to stdout before a request).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: Generous ceiling for "the gateway started" and "the gateway stopped". It is a
#: ceiling, not a delay: the test proceeds as soon as the readiness line appears,
#: so a slow machine costs nothing and a hung gateway still fails.
GATEWAY_TIMEOUT_S = 30.0


def _server_params(data_dir: Path) -> StdioServerParameters:
    env = dict(os.environ)
    env["DESKWEAVE_DATA_DIR"] = str(data_dir)
    env["DESKWEAVE_LOG_LEVEL"] = "INFO"
    env["PYTHONIOENCODING"] = "utf-8"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "deskweave_mcp_gateway"],
        env=env,
    )


async def test_stdio_client_can_call_tools(tmp_path: Path) -> None:
    async with (
        stdio_client(_server_params(tmp_path / "stdio-data")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "runtime_health" in names

        result = await session.call_tool("runtime_health", {})
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert payload["state"] == "ok"
        assert payload["schema_version"] >= 1

        tasks = await session.call_tool("list_tasks", {})
        assert json.loads(tasks.content[0].text) == []


async def test_stdout_is_not_polluted_by_logs(tmp_path: Path) -> None:
    """Nothing may reach stdout before the first protocol frame — for the whole life.

    Deterministic in both directions: the wait is for the gateway's **readiness
    line on stderr**, not for a number of seconds, and the run ends by closing
    stdin, which is the server's own cue to stop. The earlier version slept 2.5 s
    and killed the process; on this machine startup reaches that line in
    2.33-2.46 s, so it passed or failed by ~100 ms (1 pass in 4 identical runs).
    Waiting for the signal removes the constant, and closing stdin instead of
    killing also covers the shutdown path, which the fixed wait never observed.
    """
    data_dir = tmp_path / "purity-data"
    env = dict(os.environ)
    env["DESKWEAVE_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        [sys.executable, "-m", "deskweave_mcp_gateway"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout_chunks: list[bytes] = []
    stderr_lines: list[bytes] = []
    ready = threading.Event()

    def pump(stream, sink, on_line=None) -> None:
        for line in iter(stream.readline, b""):
            sink.append(line)
            if on_line is not None:
                on_line(line)
        stream.close()

    def note(line: bytes) -> None:
        if b"MCP gateway ready" in line:
            ready.set()

    readers = [
        threading.Thread(target=pump, args=(process.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, stderr_lines, note), daemon=True),
    ]
    for reader in readers:
        reader.start()

    try:
        became_ready = await asyncio.to_thread(ready.wait, GATEWAY_TIMEOUT_S)
        assert became_ready, (
            "the gateway never logged readiness on stderr within "
            f"{GATEWAY_TIMEOUT_S}s; stderr so far: "
            f"{b''.join(stderr_lines).decode('utf-8', 'replace')[:400]!r}"
        )
        # EOF on stdin is a clean stop, not a kill: the server has to finish on
        # its own for the stdout assertion to cover its whole lifetime.
        assert process.stdin is not None
        process.stdin.close()
        await asyncio.to_thread(process.wait, GATEWAY_TIMEOUT_S)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung shutdown
            process.kill()
        for reader in readers:
            reader.join(5.0)

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_lines)
    assert stdout == b"", f"stdout must stay protocol-only, got: {stdout[:400]!r}"
    assert b"MCP gateway ready" in stderr, f"expected startup log on stderr, got: {stderr[:400]!r}"


@pytest.mark.parametrize("tool", ["runtime_health", "list_adapters"])
async def test_unknown_arguments_are_rejected(tmp_path: Path, tool: str) -> None:
    """Invalid tool arguments must produce a protocol-level error, not silence."""
    async with (
        stdio_client(_server_params(tmp_path / f"args-{tool}")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool, {"unexpected": "value"})
        assert result.is_error is True
