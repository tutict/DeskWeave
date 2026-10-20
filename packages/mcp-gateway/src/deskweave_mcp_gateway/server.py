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

from deskweave_contracts import CollectionRequest, DeskWeaveError
from deskweave_runtime import Runtime

__version__ = "0.0.1"

INSTRUCTIONS = (
    "DeskWeave local runtime gateway. Tools report real runtime state and can start "
    "a reference-image collection. Confirming a selection is *not* exposed as a tool: "
    "a human must confirm it in the desktop UI, so an agent can never authorise its own "
    "Blender execution. Use get_reference_task to poll for the outcome."
)

#: Declared argument names per tool. Empty frozenset = the tool takes no arguments.
#: Keep this in sync with ``build_server``; a mismatch is caught by the tests.
TOOL_ARGUMENTS: dict[str, frozenset[str]] = {
    "runtime_health": frozenset(),
    "list_adapters": frozenset(),
    "list_capabilities": frozenset(),
    "list_tasks": frozenset(),
    "collect_reference_images": frozenset({"query", "limit", "source"}),
    "get_reference_task": frozenset({"task_id"}),
    "cancel_reference_task": frozenset({"task_id"}),
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

    @server.tool(
        name="collect_reference_images",
        description=(
            "Start collecting reference images for a query. Returns immediately with a "
            "task id; the download and the human confirmation happen asynchronously. "
            "Poll get_reference_task for progress."
        ),
    )
    async def collect_reference_images(
        query: str,
        limit: int = 6,
        source: str | None = None,
    ) -> str:
        request = CollectionRequest.model_validate(
            {"query": query, "limit": limit, **({"source": source} if source else {})}
        )
        try:
            task = await runtime.references.start_collection(request)
        except DeskWeaveError as exc:
            return _failure(exc)
        return json.dumps(
            {
                "ok": True,
                "task_id": task.task_id,
                "status": task.status.value,
                "query": request.query,
                "limit": request.limit,
                "source": request.source.value,
                "message": (
                    "Collection started. Poll get_reference_task until the status is "
                    "waiting_for_confirmation, then ask the user to confirm in DeskWeave."
                ),
            }
        )

    @server.tool(
        name="get_reference_task",
        description=(
            "Return one reference task with its candidate artifact ids, per-candidate "
            "failures and the execution outcome. Contains no filesystem paths."
        ),
    )
    async def get_reference_task(task_id: str) -> str:
        try:
            view = await runtime.references.get_view(task_id)
        except DeskWeaveError as exc:
            return _failure(exc)
        return json.dumps(_view_payload(view))

    @server.tool(
        name="cancel_reference_task",
        description="Cancel a reference task that has not finished yet.",
    )
    async def cancel_reference_task(task_id: str) -> str:
        try:
            await runtime.references.cancel(task_id)
            view = await runtime.references.get_view(task_id)
        except DeskWeaveError as exc:
            return _failure(exc)
        return json.dumps(_view_payload(view))

    return server


def _view_payload(view: Any) -> dict[str, Any]:
    """Serialise a task view for an agent: ids and metadata, never paths."""
    task = view.task
    result = task.result or {}
    return {
        "ok": task.status.value in {"waiting_for_confirmation", "succeeded"},
        "task_id": task.task_id,
        "status": task.status.value,
        "query": result.get("query"),
        "candidates": [
            {
                "artifact_id": artifact.artifact_id,
                "title": artifact.metadata.title,
                "media_type": artifact.media_type,
                "width": artifact.metadata.width,
                "height": artifact.metadata.height,
                "source_page": artifact.metadata.source_page,
            }
            for artifact in view.artifacts
        ],
        "failures": result.get("failures", []),
        "confirmation": result.get("confirmation"),
        "execution": _public_execution(result.get("execution")),
        "error": task.error.model_dump(mode="json") if task.error else None,
        "awaiting_user_confirmation": task.status.value == "waiting_for_confirmation",
    }


def _public_execution(execution: Any) -> dict[str, Any] | None:
    """Strip absolute paths from the execution summary before it leaves the process."""
    if not isinstance(execution, dict):
        return None
    public = {key: value for key, value in execution.items() if key not in {"blend_file"}}
    return public


def _failure(error: Any) -> str:
    info = error.to_info()
    return json.dumps({"ok": False, "error": info.model_dump(mode="json")})


async def run_on_streams(server: MCPServer, read_stream: Any, write_stream: Any) -> None:
    """Run ``server`` on an arbitrary pair of MCP streams.

    This mirrors the SDK's stdio runner and lets the gateway be embedded on a
    custom transport (used by the in-memory protocol tests).
    """
    lowlevel = server._lowlevel_server
    await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())


__all__ = ["INSTRUCTIONS", "TOOL_ARGUMENTS", "StrictMCPServer", "build_server", "run_on_streams"]
