"""Real MCP protocol test over in-memory streams.

This exercises the actual MCP request/response layer (initialize, list_tools,
call_tool) using the official client session and the gateway's server, without
spawning a subprocess. It therefore runs everywhere, including sandboxes that
block named pipes.
"""

from __future__ import annotations

import json

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from deskweave_mcp_gateway import build_server, run_on_streams
from deskweave_runtime import Runtime


async def test_protocol_initialize_list_and_call(runtime: Runtime) -> None:
    server = build_server(runtime)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_on_streams, server, server_read, server_write)
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "runtime_health",
                    "list_adapters",
                    "list_capabilities",
                    "list_tasks",
                } <= names

                result = await session.call_tool("runtime_health", {})
                assert result.is_error is False
                payload = json.loads(result.content[0].text)
                assert payload["state"] == "ok"
                assert payload["schema_version"] >= 1

                adapters = await session.call_tool("list_adapters", {})
                assert json.loads(adapters.content[0].text) == []
            task_group.cancel_scope.cancel()


async def test_protocol_unknown_tool_reports_error(runtime: Runtime) -> None:
    server = build_server(runtime)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_on_streams, server, server_read, server_write)
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                result = await session.call_tool("does_not_exist", {})
                assert result.is_error is True
            task_group.cancel_scope.cancel()
