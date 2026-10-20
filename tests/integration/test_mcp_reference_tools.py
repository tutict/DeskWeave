"""MCP tools for the reference workflow, over real in-memory MCP streams.

The browser and Blender connectors are fakes, but the MCP transport, the tool
schemas, the runtime service and the permission layer are real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from deskweave_contracts import CollectionRequest, ConfirmRequest
from deskweave_mcp_gateway import build_server, run_on_streams
from deskweave_runtime import Runtime
from support.doubles import FakeBrowser, candidates, install_fakes


async def _with_session(runtime: Runtime, body) -> None:
    server = build_server(runtime)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_on_streams, server, server_read, server_write)
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                await body(session)
            task_group.cancel_scope.cancel()


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def _collect(session: ClientSession, query: str = "anime reference") -> dict:
    result = await session.call_tool("collect_reference_images", {"query": query, "limit": 2})
    assert result.is_error is False, result
    return _payload(result)


async def _wait_for_settled(session: ClientSession, task_id: str) -> dict:
    for _ in range(100):
        payload = _payload(await session.call_tool("get_reference_task", {"task_id": task_id}))
        if payload["status"] not in {"running", "pending"}:
            return payload
        await asyncio.sleep(0.05)
    raise AssertionError("task did not settle")


async def test_collect_returns_immediately_with_a_task_id(runtime: Runtime) -> None:
    browser, _, _ = await install_fakes(runtime)
    browser.candidates = candidates(2)

    async def body(session: ClientSession) -> None:
        payload = await _collect(session)
        assert payload["ok"] is True
        assert payload["task_id"].startswith("task_")
        assert payload["status"] in {"running", "pending"}
        assert "Poll get_reference_task" in payload["message"]

    await _with_session(runtime, body)


async def test_collected_candidates_are_visible_by_id_only(
    runtime: Runtime, data_dir: Path
) -> None:
    browser, _, _ = await install_fakes(runtime)
    browser.candidates = candidates(2)

    async def body(session: ClientSession) -> None:
        started = await _collect(session)
        settled = await _wait_for_settled(session, started["task_id"])

        assert settled["status"] == "waiting_for_confirmation"
        assert settled["awaiting_user_confirmation"] is True
        assert len(settled["candidates"]) == 2
        for candidate in settled["candidates"]:
            assert candidate["artifact_id"].startswith("art_")
            assert candidate["media_type"] == "image/png"
            assert candidate["source_page"].startswith("https://example.com/")
        serialised = json.dumps(settled)
        assert str(data_dir) not in serialised
        assert "uri" not in serialised

    await _with_session(runtime, body)


async def test_unknown_arguments_are_rejected_by_the_tool(runtime: Runtime) -> None:
    await install_fakes(runtime)

    async def body(session: ClientSession) -> None:
        result = await session.call_tool(
            "collect_reference_images", {"query": "anime", "confirmed": True}
        )
        assert result.is_error is True
        assert "confirmed" in result.content[0].text

    await _with_session(runtime, body)


async def test_get_reference_task_reports_unknown_ids_as_data(runtime: Runtime) -> None:
    await install_fakes(runtime)

    async def body(session: ClientSession) -> None:
        result = await session.call_tool("get_reference_task", {"task_id": "task_missing"})
        # A business error is data, not a protocol failure: the agent can reason
        # about it and retry with another id.
        assert result.is_error is False
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "not_found"

    await _with_session(runtime, body)


async def test_cancel_is_exposed_but_confirm_is_not(runtime: Runtime) -> None:
    browser, _, _ = await install_fakes(runtime)
    browser.candidates = candidates(1)

    async def body(session: ClientSession) -> None:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "cancel_reference_task" in names
        assert not any("confirm" in name for name in names)

        started = await _collect(session)
        await _wait_for_settled(session, started["task_id"])
        cancelled = _payload(
            await session.call_tool("cancel_reference_task", {"task_id": started["task_id"]})
        )
        assert cancelled["status"] == "cancelled"

    await _with_session(runtime, body)


async def test_confirmed_execution_never_leaks_a_path(runtime: Runtime, data_dir: Path) -> None:
    browser, blender, _ = await install_fakes(runtime)
    browser.candidates = candidates(1)

    started = await runtime.references.start_collection(
        CollectionRequest(query="anime reference", limit=1)
    )
    settled = await runtime.references.wait_until_settled(started.task_id)
    await runtime.references.confirm(
        settled.task_id,
        ConfirmRequest(artifact_ids=list(settled.result["candidates"])),
    )

    async def body(session: ClientSession) -> None:
        payload = _payload(
            await session.call_tool("get_reference_task", {"task_id": settled.task_id})
        )
        assert payload["status"] == "succeeded"
        assert payload["execution"]["ok"] is True
        assert "blend_file" not in payload["execution"]
        serialised = json.dumps(payload)
        assert str(data_dir) not in serialised
        assert "C:\\" not in serialised

    await _with_session(runtime, body)
    assert blender.calls


async def test_the_tool_forwards_the_source_to_the_connector_that_serves_it(
    runtime: Runtime,
) -> None:
    """The gateway stays source-agnostic: it forwards the name, the runtime resolves it.

    This is the payoff of Phase 2 — adding a source needed no gateway change, so the
    protocol surface is unchanged and only the wiring differs.
    """
    browser, _, _ = await install_fakes(runtime)
    browser.candidates = candidates(1)
    wikimedia = FakeBrowser(adapter_id="wikimedia", default_source="wikimedia")
    wikimedia.candidates = candidates(1)
    await runtime.register_adapter(wikimedia)
    runtime.register_collection_source("wikimedia", "wikimedia")

    async def body(session: ClientSession) -> None:
        payload = _payload(
            await session.call_tool(
                "collect_reference_images",
                {"query": "elf armor", "limit": 1, "source": "wikimedia"},
            )
        )
        assert payload["ok"] is True, payload
        assert payload["source"] == "wikimedia"
        settled = await _wait_for_settled(session, payload["task_id"])
        assert settled["status"] == "waiting_for_confirmation"
        assert len(settled["candidates"]) == 1
        assert wikimedia.calls == 1
        assert browser.calls == 0, "the default source must not be consulted as a fallback"

    await _with_session(runtime, body)


async def test_a_source_without_a_connector_is_reported_not_crashed(
    runtime: Runtime,
) -> None:
    """An unconfigured source is a configuration fact, not a protocol failure."""
    browser, _, _ = await install_fakes(runtime)

    async def body(session: ClientSession) -> None:
        result = await session.call_tool(
            "collect_reference_images",
            {"query": "elf armor", "limit": 1, "source": "wikimedia"},
        )
        assert result.is_error is False, "the session must stay usable"
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "adapter_connection_error"
        assert payload["error"]["details"]["source"] == "wikimedia"
        # The tool still works afterwards.
        started = await _collect(session)
        assert started["ok"] is True
        assert started["source"] == "bing"
        await _wait_for_settled(session, started["task_id"])

    await _with_session(runtime, body)
    assert browser.calls == 1, "only the healthy request reached the browser connector"


async def test_missing_required_argument_is_a_protocol_error(runtime: Runtime) -> None:
    await install_fakes(runtime)

    async def body(session: ClientSession) -> None:
        result = await session.call_tool("get_reference_task", {})
        assert result.is_error is True

    await _with_session(runtime, body)
