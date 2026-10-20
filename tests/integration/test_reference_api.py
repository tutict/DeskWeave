"""Integration tests for the control API's reference-workflow routes.

These run against a real loopback HTTP server, so they cover routing, auth,
status-code mapping, JSON shapes and the binary preview endpoint — the exact
contract the desktop UI is written against.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.service import RuntimeService
from support.doubles import candidates, install_fakes

TOKEN = "test-token"


def _call(
    url: str,
    *,
    token: str | None = TOKEN,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json(payload: bytes) -> dict[str, Any]:
    return json.loads(payload.decode("utf-8"))


@pytest.fixture
async def api(config: RuntimeConfig):
    runtime = await Runtime.create(config)
    await runtime.start()
    _, blender, _ = await install_fakes(runtime)
    service = RuntimeService(runtime, token=TOKEN, host=config.host, port=0)
    await service.start()
    try:
        yield runtime, service, blender
    finally:
        await service.stop()
        await runtime.close()


async def _start_task(runtime: Runtime, service: RuntimeService, *, limit: int = 2) -> str:
    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "anime character reference", "limit": limit},
    )
    assert status == 202, body
    task_id = _json(body)["task"]["task_id"]
    await runtime.references.wait_until_settled(task_id)
    return task_id


# -- collection -----------------------------------------------------------
async def test_create_task_returns_202_and_location(api) -> None:
    runtime, service, _ = api
    browser = runtime.registry.get("browser")
    browser.candidates = candidates(2)

    status, headers, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "anime character reference", "limit": 2},
    )

    assert status == 202
    payload = _json(body)
    assert payload["task"]["status"] == "running"
    assert payload["artifacts"] == []
    task_id = payload["task"]["task_id"]
    assert headers["Location"] == f"/tasks/{task_id}"

    await runtime.references.wait_until_settled(task_id)


async def test_create_task_rejects_unknown_field(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "anime", "limit": 2, "confirmed": True},
    )

    assert status == 400
    error = _json(body)["error"]
    assert error["code"] == "validation_error"
    assert any("confirmed" in message for message in error["details"]["errors"])


async def test_create_task_rejects_empty_query(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/tasks", method="POST", body={"query": ""}
    )
    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"


async def test_create_task_rejects_invalid_json(api) -> None:
    _, service, _ = api
    request = urllib.request.Request(f"{service.url}/tasks", data=b"{oops", method="POST")
    request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()

    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"


async def test_task_view_exposes_candidates_without_paths(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(2)
    task_id = await _start_task(runtime, service, limit=2)

    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    payload = _json(body)

    assert status == 200
    assert payload["task"]["status"] == "waiting_for_confirmation"
    assert len(payload["artifacts"]) == 2
    for artifact in payload["artifacts"]:
        assert artifact["artifact_id"].startswith("art_")
        assert artifact["metadata"]["title"].startswith("Reference")
        assert "uri" not in artifact
        assert str(runtime.config.data_dir) not in body.decode("utf-8")


async def test_task_list_can_filter_by_status(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)

    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/tasks?status=waiting_for_confirmation"
    )
    waiting = _json(body)["tasks"]
    assert status == 200
    assert [item["task"]["task_id"] for item in waiting] == [task_id]

    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks?status=failed")
    assert status == 200
    assert _json(body)["tasks"] == []


async def test_unknown_status_filter_is_a_client_error(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks?status=nope")
    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"


async def test_unknown_task_returns_404(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks/task_missing")
    assert status == 404
    assert _json(body)["error"]["code"] == "not_found"


async def test_malformed_task_id_is_not_routed(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks/..%2F..%2Fetc%2Fpasswd")
    assert status == 404
    assert _json(body)["error"]["code"] == "not_found"


# -- confirmation ---------------------------------------------------------
async def test_confirm_executes_the_target(api) -> None:
    runtime, service, blender = api
    runtime.registry.get("browser").candidates = candidates(2)
    task_id = await _start_task(runtime, service, limit=2)
    _, _, view_body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    selected = [item["artifact_id"] for item in _json(view_body)["artifacts"]]

    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks/{task_id}/confirm",
        method="POST",
        body={"artifact_ids": selected, "plane_name": "AnimeRef"},
    )
    payload = _json(body)

    assert status == 200
    assert payload["task"]["status"] == "succeeded"
    assert payload["task"]["result"]["confirmation"]["artifact_ids"] == selected
    assert payload["task"]["result"]["execution"]["plane"]["name"] == "AnimeRef"
    assert blender.calls == [selected]

    # A second confirm is idempotent: still 200, no second execution.
    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks/{task_id}/confirm",
        method="POST",
        body={"artifact_ids": selected},
    )
    assert status == 200
    assert _json(body)["task"]["status"] == "succeeded"
    assert len(blender.calls) == 1


async def test_confirm_with_foreign_artifact_is_forbidden(api) -> None:
    runtime, service, blender = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)

    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks/{task_id}/confirm",
        method="POST",
        body={"artifact_ids": ["art_not_mine"]},
    )

    assert status == 403
    assert _json(body)["error"]["code"] == "permission_denied"
    assert blender.calls == []


async def test_confirm_without_artifacts_is_rejected(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)

    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/tasks/{task_id}/confirm", method="POST", body={"artifact_ids": []}
    )
    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"


async def test_confirm_with_confirmed_flag_grants_nothing(api) -> None:
    runtime, service, blender = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)
    _, _, view_body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    selected = [item["artifact_id"] for item in _json(view_body)["artifacts"]]

    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks/{task_id}/confirm",
        method="POST",
        body={"artifact_ids": selected, "confirmed": True},
    )

    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"
    assert blender.calls == []


# -- cancellation ---------------------------------------------------------
async def test_cancel_while_waiting(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)

    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/tasks/{task_id}/cancel", method="POST"
    )

    assert status == 200
    assert _json(body)["task"]["status"] == "cancelled"


async def test_cancel_after_success_conflicts(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)
    _, _, view_body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    selected = [item["artifact_id"] for item in _json(view_body)["artifacts"]]
    await asyncio.to_thread(
        _call,
        f"{service.url}/tasks/{task_id}/confirm",
        method="POST",
        body={"artifact_ids": selected},
    )

    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/tasks/{task_id}/cancel", method="POST"
    )
    assert status == 409
    assert _json(body)["error"]["code"] == "conflict"


# -- artifacts ------------------------------------------------------------
async def test_preview_serves_real_bytes(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)
    _, _, view_body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    artifact = _json(view_body)["artifacts"][0]

    status, headers, body = await asyncio.to_thread(
        _call, f"{service.url}/artifacts/{artifact['artifact_id']}/preview"
    )

    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(body) == artifact["size_bytes"]


async def test_artifact_metadata_endpoint_hides_the_path(api) -> None:
    runtime, service, _ = api
    runtime.registry.get("browser").candidates = candidates(1)
    task_id = await _start_task(runtime, service, limit=1)
    _, _, view_body = await asyncio.to_thread(_call, f"{service.url}/tasks/{task_id}")
    artifact = _json(view_body)["artifacts"][0]

    status, _, body = await asyncio.to_thread(
        _call, f"{service.url}/artifacts/{artifact['artifact_id']}"
    )
    payload = _json(body)

    assert status == 200
    assert payload["artifact"]["artifact_id"] == artifact["artifact_id"]
    assert "uri" not in payload["artifact"]
    assert str(runtime.config.data_dir) not in body.decode("utf-8")


async def test_preview_of_unknown_artifact_is_404(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(_call, f"{service.url}/artifacts/art_missing/preview")
    assert status == 404
    assert _json(body)["error"]["code"] == "not_found"


# -- transport guards -----------------------------------------------------
async def test_missing_token_is_401(api) -> None:
    _, service, _ = api
    status, _, body = await asyncio.to_thread(_call, f"{service.url}/tasks", token=None)
    assert status == 401
    assert _json(body)["error"]["code"] == "unauthorized"


async def test_wrong_token_is_401(api) -> None:
    _, service, _ = api
    status, _, _ = await asyncio.to_thread(_call, f"{service.url}/tasks", token="nope")
    assert status == 401


async def test_oversized_body_is_rejected_before_reading(api) -> None:
    """A body larger than the cap must be refused from the header alone.

    Only the headers are sent: if the server tried to read the body first, the
    test would hang instead of getting a 400, which is exactly the behaviour the
    size cap is supposed to prevent.
    """
    _, service, _ = api
    host, port = service.url.removeprefix("http://").split(":")
    request = (
        f"POST /tasks HTTP/1.1\r\nHost: {host}\r\n"
        f"Authorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {200 * 1024}\r\nConnection: close\r\n\r\n"
    ).encode("ascii")

    def _send() -> bytes:
        chunks: list[bytes] = []
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.sendall(request)
            while True:
                with contextlib.suppress(OSError):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    continue
                break
        return b"".join(chunks)

    raw = await asyncio.to_thread(_send)
    assert raw, "the server closed the connection without answering"
    assert b"400" in raw.split(b"\r\n", 1)[0]
    assert b"validation_error" in raw


# -- source routing -------------------------------------------------------
async def test_a_registered_source_is_served_by_its_own_connector(api) -> None:
    """The workflow routes a source to a connector; it does not know their names."""
    from support.doubles import FakeBrowser

    runtime, service, _ = api
    browser = runtime.registry.get("browser")
    browser.candidates = candidates(1)
    wikimedia = FakeBrowser(
        candidates=candidates(2), adapter_id="wikimedia", default_source="wikimedia"
    )
    await runtime.register_adapter(wikimedia)
    runtime.register_collection_source("wikimedia", "wikimedia")

    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "elf armor", "limit": 2, "source": "wikimedia"},
    )
    assert status == 202, body
    task_id = _json(body)["task"]["task_id"]
    await runtime.references.wait_until_settled(task_id)

    view = await runtime.references.get_view(task_id)
    assert view.task.status.value == "waiting_for_confirmation"
    assert len(view.artifacts) == 2
    assert view.task.result["request"]["source"] == "wikimedia"
    assert wikimedia.calls == 1
    assert browser.calls == 0, "the browser connector must not be asked for a wikimedia source"


async def test_a_source_without_a_connector_is_a_configuration_error(api) -> None:
    """No silent fallback to a different source: the caller is told what is missing."""
    _runtime, service, _ = api
    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "elf armor", "source": "wikimedia"},
    )
    assert status == 503
    payload = _json(body)
    assert payload["error"]["code"] == "adapter_connection_error"
    assert payload["error"]["details"]["source"] == "wikimedia"


async def test_an_unknown_source_is_rejected_at_the_edge(api) -> None:
    _runtime, service, _ = api
    status, _, body = await asyncio.to_thread(
        _call,
        f"{service.url}/tasks",
        method="POST",
        body={"query": "elf armor", "source": "duckduckgo"},
    )
    assert status == 400
    assert _json(body)["error"]["code"] == "validation_error"


async def test_attribution_from_a_source_reaches_the_artifact(api) -> None:
    """A CC source must not lose its licence on the way to the artifact."""
    runtime, service, _ = api
    browser = runtime.registry.get("browser")
    browser.candidates = [
        {
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/a.png",
            "page_url": "https://commons.wikimedia.org/wiki/File:A.png",
            "title": "File:A.png",
            "thumbnail_url": "https://upload.wikimedia.org/wikipedia/commons/a.png",
            "width": 1600,
            "height": 900,
            "license": "CC BY-SA 4.0",
            "artist": "Someone",
        }
    ]

    task_id = await _start_task(runtime, service, limit=1)
    view = await runtime.references.get_view(task_id)
    metadata = view.artifacts[0].metadata
    assert metadata.extra["license"] == "CC BY-SA 4.0"
    assert metadata.extra["artist"] == "Someone"
    assert metadata.extra["thumbnail_url"].startswith("https://upload.wikimedia.org/")
