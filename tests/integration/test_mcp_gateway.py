"""End-to-end MCP test: spawn the gateway over stdio and call a tool.

This is the full transport path (subprocess + stdio). Some sandboxes block the
named pipes that asyncio subprocesses need; when that happens the tests skip with
an explicit reason instead of failing, because the in-memory protocol test
(``test_mcp_protocol.py``) already covers the protocol layer.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _subprocess_available() -> bool:
    """Probe whether asyncio can create a fully-piped subprocess.

    Some sandboxes deny the named pipes that asyncio uses for piped stdio.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.exit(0)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.wait()
    except (PermissionError, OSError):
        return False
    return True


@pytest.fixture
async def require_subprocess() -> None:
    if not await _subprocess_available():
        pytest.skip("asyncio subprocesses are unavailable in this environment")


def _params(python_executable: str, repo_root: Path, env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=python_executable,
        args=["-m", "deskweave_mcp_gateway"],
        env=env,
        cwd=str(repo_root),
    )


async def test_list_tools(
    python_executable: str,
    repo_root: Path,
    subprocess_env: dict[str, str],
    require_subprocess: None,
) -> None:
    params = _params(python_executable, repo_root, subprocess_env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "runtime_health",
            "list_adapters",
            "list_capabilities",
            "list_tasks",
            "collect_reference_images",
            "get_reference_task",
            "cancel_reference_task",
        } <= names
        # Confirmation must never be reachable from an agent.
        assert not any("confirm" in name for name in names)


async def test_runtime_health_tool_returns_real_state(
    python_executable: str,
    repo_root: Path,
    subprocess_env: dict[str, str],
    require_subprocess: None,
) -> None:
    params = _params(python_executable, repo_root, subprocess_env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("runtime_health", {})
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert payload["state"] == "ok"
        assert payload["schema_version"] >= 1


async def test_list_adapters_tool_reports_the_shipped_connectors(
    python_executable: str,
    repo_root: Path,
    subprocess_env: dict[str, str],
    require_subprocess: None,
) -> None:
    params = _params(python_executable, repo_root, subprocess_env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("list_adapters", {})
        adapters = json.loads(result.content[0].text)
        ids = {adapter["adapter_id"] for adapter in adapters}
        # The shipped set. `filesystem` is not registered by bootstrap (it is
        # opt-in: it needs an allowed root), so it is absent on purpose.
        assert ids == {"browser", "blender", "wikimedia"}
        for adapter in adapters:
            assert adapter["capabilities"], adapter["adapter_id"]
            # Descriptors must never carry local paths.
            assert str(repo_root) not in json.dumps(adapter)


async def test_unknown_tool_reports_error(
    python_executable: str,
    repo_root: Path,
    subprocess_env: dict[str, str],
    require_subprocess: None,
) -> None:
    params = _params(python_executable, repo_root, subprocess_env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("does_not_exist", {})
        assert result.is_error is True
