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
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
    try:
        # The server waits for an ``initialize`` frame; nothing may be written to
        # stdout in the meantime, otherwise MCP framing is corrupted.
        await asyncio.sleep(2.5)
        process.kill()
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

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
