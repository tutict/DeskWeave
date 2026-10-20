"""Full-chain end-to-end test with real components.

    DESKWEAVE_NETWORK_TESTS=1 DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/e2e

What actually runs here:

1. a real MCP session (in-memory transport) calls ``collect_reference_images``;
2. the real browser connector opens cn.bing.com and parses real result cards;
3. the real artifact store downloads and validates real image bytes to disk;
4. the task parks in ``waiting_for_confirmation`` — nothing has touched Blender;
5. the real control API serves the task view and the image preview;
6. ``POST /tasks/{id}/confirm`` runs a real headless Blender, which builds the
   planes and saves a ``.blend`` that a second Blender process re-opens;
7. the MCP tool reports the outcome without leaking a single path.

If any step cannot run (no network, no Blender, site blocks the request), the test
fails loudly rather than degrading into a mocked success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from deskweave_mcp_gateway import build_server, run_on_streams
from deskweave_runtime import Runtime
from deskweave_runtime.bootstrap import register_default_adapters
from deskweave_runtime.service import RuntimeService

pytestmark = [
    pytest.mark.network,
    pytest.mark.blender,
    pytest.mark.skipif(
        os.environ.get("DESKWEAVE_NETWORK_TESTS") != "1"
        or os.environ.get("DESKWEAVE_BLENDER_TESTS") != "1",
        reason="needs both DESKWEAVE_NETWORK_TESTS=1 and DESKWEAVE_BLENDER_TESTS=1",
    ),
]

TOKEN = "e2e-token"
QUERY = "anime character turnaround sheet"


def _http(
    url: str, *, method: str = "GET", body: dict | None = None
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


async def _wait_for_task(session: ClientSession, task_id: str, *, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await session.call_tool("get_reference_task", {"task_id": task_id})
        payload = json.loads(result.content[0].text)
        if payload["status"] not in {"running", "pending"}:
            return payload
        await asyncio.sleep(0.5)
    raise AssertionError(f"task {task_id} did not settle within {timeout}s")


def _is_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or data.startswith(b"BM")
    )


async def test_real_chain_collect_confirm_blender(runtime: Runtime, data_dir: Path) -> None:
    await register_default_adapters(runtime, logger=logging.getLogger("e2e"))
    service = RuntimeService(runtime, token=TOKEN, host=runtime.config.host, port=0)
    await service.start()
    server = build_server(runtime)

    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(run_on_streams, server, server_read, server_write)
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()

                    # 1. The agent starts the collection and gets a task id at once.
                    started = await session.call_tool(
                        "collect_reference_images", {"query": QUERY, "limit": 2}
                    )
                    assert started.is_error is False, started
                    started_payload = json.loads(started.content[0].text)
                    task_id = started_payload["task_id"]
                    assert started_payload["status"] in {"running", "pending"}

                    # 2-4. Real browser + real download + parked task.
                    waiting = await _wait_for_task(session, task_id)
                    # A real search can legitimately fail (blocked page, every
                    # download refused). Report *why* instead of a truncated repr,
                    # otherwise a transient network hiccup looks like a code bug.
                    assert waiting["status"] == "waiting_for_confirmation", (
                        f"status={waiting['status']} error={waiting.get('error')} "
                        f"failures={waiting.get('failures')}"
                    )
                    assert waiting["awaiting_user_confirmation"] is True
                    candidate_ids = [item["artifact_id"] for item in waiting["candidates"]]
                    assert candidate_ids, "the real search produced no downloadable candidate"
                    assert all(
                        item["media_type"].startswith("image/") for item in waiting["candidates"]
                    )

                    # 5. The control API (what the desktop UI uses) shows the same task.
                    status, _, view_body = await asyncio.to_thread(
                        _http, f"{service.url}/tasks/{task_id}"
                    )
                    assert status == 200
                    view = json.loads(view_body)
                    assert view["task"]["status"] == "waiting_for_confirmation"
                    assert [item["artifact_id"] for item in view["artifacts"]] == candidate_ids
                    assert "uri" not in view["artifacts"][0]

                    status, headers, image = await asyncio.to_thread(
                        _http, f"{service.url}/artifacts/{candidate_ids[0]}/preview"
                    )
                    assert status == 200
                    assert headers["Content-Type"].startswith("image/")
                    assert len(image) == view["artifacts"][0]["size_bytes"]
                    assert _is_image(image)

                    # 6. Confirmation → real Blender.
                    status, _, confirm_body = await asyncio.to_thread(
                        _http,
                        f"{service.url}/tasks/{task_id}/confirm",
                        method="POST",
                        body={"artifact_ids": candidate_ids[:1], "plane_name": "E2EPlane"},
                    )
                    confirmed = json.loads(confirm_body)
                    assert status == 200, confirmed
                    assert confirmed["task"]["status"] == "succeeded", confirmed["task"].get(
                        "error"
                    )
                    execution = confirmed["task"]["result"]["execution"]
                    assert execution["ok"] is True
                    assert execution["verification"]["opened"] is True
                    assert execution["plane"]["name"] == "E2EPlane"
                    assert execution["plane"]["image_count"] == 1

                    # 7. The MCP view agrees and leaks nothing.
                    final = await _wait_for_task(session, task_id)
                    assert final["status"] == "succeeded"
                    assert final["execution"]["ok"] is True
                    assert "blend_file" not in final["execution"]
                    serialised = json.dumps(final)
                    assert str(data_dir) not in serialised
                    assert "file://" not in serialised

                    # The produced .blend is a registered artifact of this task.
                    blend_id = confirmed["task"]["result"]["execution"].get("blend_artifact_id")
                    assert blend_id
                    stored = await runtime.artifacts.get(blend_id)
                    path = await runtime.artifacts.resolve_path(blend_id)
                    assert stored.task_id == task_id
                    assert stored.kind.value == "model"
                    assert path.is_file() and path.suffix == ".blend"

                    # A second confirmation is idempotent: same task, no re-execution.
                    status, _, again_body = await asyncio.to_thread(
                        _http,
                        f"{service.url}/tasks/{task_id}/confirm",
                        method="POST",
                        body={"artifact_ids": candidate_ids[:1]},
                    )
                    assert status == 200
                    assert json.loads(again_body)["task"]["status"] == "succeeded"

                task_group.cancel_scope.cancel()
    finally:
        await service.stop()
