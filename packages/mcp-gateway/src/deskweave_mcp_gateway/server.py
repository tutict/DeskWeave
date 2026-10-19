"""MCP server exposing DeskWeave runtime tools.

The gateway depends on the runtime but the runtime does not depend on the
gateway, so protocol handling stays decoupled from core business logic.

Extension point for future, side-effecting tools: every such tool must call
``runtime.execute(...)`` (which runs the permission layer) instead of touching
adapters or the database directly.

Argument handling is deliberately strict: the MCP SDK ignores unknown keys, and a
gateway that silently drops arguments is a security problem (a caller can believe
it asked for something it did not). :class:`StrictMCPServer` rejects undeclared
arguments *and* validates them against the tool's published JSON Schema before
dispatch.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from deskweave_runtime import Runtime

__version__ = "0.0.1"

INSTRUCTIONS = (
    "DeskWeave local runtime gateway. Tools are read-only in this phase and "
    "report real runtime state. Side-effecting tools are not yet exposed."
)

#: Declared argument names per tool. Empty frozenset = the tool takes no arguments.
#: Keep this in sync with ``build_server``; a mismatch is caught by the tests.
TOOL_ARGUMENTS: dict[str, frozenset[str]] = {
    "runtime_health": frozenset(),
    "list_adapters": frozenset(),
    "list_capabilities": frozenset(),
    "list_tasks": frozenset(),
}


class StrictMCPServer(MCPServer):
    """An :class:`MCPServer` that rejects arguments the tool did not declare."""

    def __init__(self, *args: Any, argument_specs: dict[str, frozenset[str]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._argument_specs = argument_specs

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        declared = self._argument_specs.get(name)
        if declared is not None:
            unknown = sorted(set(arguments) - declared)
            if unknown:
                raise ToolError(f"tool {name!r} does not accept argument(s): {', '.join(unknown)}")
            schema = self._tool_input_schema(name)
            if schema:
                try:
                    Draft202012Validator(schema).validate(arguments)
                except JsonSchemaValidationError as exc:
                    raise ToolError(f"invalid arguments for tool {name!r}: {exc.message}") from exc
        return await super().call_tool(name, arguments, context)


def build_server(runtime: Runtime, *, name: str = "deskweave") -> MCPServer:
    """Build an :class:`MCPServer` bound to ``runtime``."""
    server = StrictMCPServer(
        name=name,
        version=__version__,
        instructions=INSTRUCTIONS,
        argument_specs=TOOL_ARGUMENTS,
    )

    @server.tool(
        name="runtime_health",
        description="Return the real health of the DeskWeave runtime.",
    )
    async def runtime_health() -> str:
        health = await runtime.health()
        return health.model_dump_json()

    @server.tool(
        name="list_adapters",
        description="List registered adapter descriptors.",
    )
    async def list_adapters() -> str:
        descriptors = await runtime.list_adapters()
        return json.dumps([descriptor.model_dump(mode="json") for descriptor in descriptors])

    @server.tool(
        name="list_capabilities",
        description="List every capability offered by registered adapters.",
    )
    async def list_capabilities() -> str:
        descriptors = await runtime.list_adapters()
        capabilities = [
            {"adapter_id": descriptor.adapter_id, **capability.model_dump(mode="json")}
            for descriptor in descriptors
            for capability in descriptor.capabilities
        ]
        return json.dumps(capabilities)

    @server.tool(
        name="list_tasks",
        description="List persisted workflow tasks.",
    )
    async def list_tasks() -> str:
        tasks = await runtime.workflow.list_tasks()
        return json.dumps([task.model_dump(mode="json") for task in tasks])

    return server


async def run_on_streams(server: MCPServer, read_stream: Any, write_stream: Any) -> None:
    """Run ``server`` on an arbitrary pair of MCP streams.

    This mirrors the SDK's stdio runner and lets the gateway be embedded on a
    custom transport (used by the in-memory protocol tests).
    """
    lowlevel = server._lowlevel_server
    await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())


__all__ = ["INSTRUCTIONS", "TOOL_ARGUMENTS", "StrictMCPServer", "build_server", "run_on_streams"]
