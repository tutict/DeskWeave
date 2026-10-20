"""Integration test: the local control API over a real socket."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from deskweave_adapter_sdk import MockAdapter
from deskweave_contracts import ArtifactKind
from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.artifacts import ArtifactOpener, LaunchPlan
from deskweave_runtime.service import RuntimeService

TOKEN = "test-token"


def _request(
    url: str, token: str | None, method: str = "GET", body: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def _service(config: RuntimeConfig) -> tuple[Runtime, RuntimeService]:
    runtime = await Runtime.create(config)
    await runtime.start()
    await runtime.register_adapter(MockAdapter())
    service = RuntimeService(runtime, token=TOKEN, host=config.host, port=0)
    await service.start()
    return runtime, service


async def test_health_requires_token(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, _ = await asyncio.to_thread(_request, f"{service.url}/health", None)
        assert status == 401
    finally:
        await service.stop()
        await runtime.close()


async def test_health_with_token(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, payload = await asyncio.to_thread(_request, f"{service.url}/health", TOKEN)
        assert status == 200
        assert payload["state"] == "ok"
        assert payload["adapters"][0]["adapter_id"] == "mock"
    finally:
        await service.stop()
        await runtime.close()


async def test_adapters_and_tasks_endpoints(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, adapters = await asyncio.to_thread(_request, f"{service.url}/adapters", TOKEN)
        assert status == 200
        assert adapters["adapters"][0]["adapter_id"] == "mock"

        status, tasks = await asyncio.to_thread(_request, f"{service.url}/tasks", TOKEN)
        assert status == 200
        assert tasks["tasks"] == []
    finally:
        await service.stop()
        await runtime.close()


async def test_unknown_route_returns_404(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, _ = await asyncio.to_thread(_request, f"{service.url}/nope", TOKEN)
        assert status == 404
    finally:
        await service.stop()
        await runtime.close()


async def test_shutdown_endpoint(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, payload = await asyncio.to_thread(
            _request, f"{service.url}/shutdown", TOKEN, "POST"
        )
        assert status == 200
        assert payload["status"] == "shutting_down"
        await asyncio.wait_for(service.wait_shutdown(), timeout=5)
    finally:
        await service.stop()
        await runtime.close()


async def test_open_artifact_route_never_leaks_a_path(config: RuntimeConfig) -> None:
    """The UI asks for an id + action; the answer is an acknowledgement, not a path."""
    runtime, service = await _service(config)
    try:
        path = config.data_dir / "artifacts" / "plane.blend"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"BLENDER-fake")
        artifact = await runtime.artifacts.register(
            kind=ArtifactKind.MODEL,
            uri=path.resolve().as_uri(),
            media_type="application/x-blender",
            size_bytes=12,
            task_id="task_owner",
        )
        plans: list[LaunchPlan] = []
        runtime._opener = ArtifactOpener(  # type: ignore[attr-defined]
            runtime.artifacts, platform="win32", launch=plans.append
        )

        status, payload = await asyncio.to_thread(
            _request,
            f"{service.url}/artifacts/{artifact.artifact_id}/open",
            TOKEN,
            "POST",
            {"action": "reveal"},
        )
        assert status == 200
        assert payload["artifact"]["opened"] is True
        assert payload["artifact"]["action"] == "reveal"
        assert payload["artifact"]["kind"] == "model"
        assert str(path.parent) not in json.dumps(payload)
        assert "file://" not in json.dumps(payload)
        assert len(plans) == 1

        # An unknown action is rejected before anything is launched.
        status, payload = await asyncio.to_thread(
            _request,
            f"{service.url}/artifacts/{artifact.artifact_id}/open",
            TOKEN,
            "POST",
            {"action": "delete"},
        )
        assert status == 400
        assert payload["error"]["code"] == "validation_error"
        assert payload["error"]["details"]["allowed"] == ["open", "reveal"]
        assert len(plans) == 1

        # Opening is not a GET route, and unknown artifacts are not "opened".
        status, _ = await asyncio.to_thread(
            _request, f"{service.url}/artifacts/{artifact.artifact_id}/open", TOKEN
        )
        assert status == 404
        status, payload = await asyncio.to_thread(
            _request,
            f"{service.url}/artifacts/art_missing/open",
            TOKEN,
            "POST",
            {"action": "reveal"},
        )
        assert status == 404
        assert payload["error"]["code"] == "not_found"
        assert len(plans) == 1
    finally:
        await service.stop()
        await runtime.close()


# -- CORS is not authentication -------------------------------------------
LOOPBACK_ORIGIN = "http://localhost:1420"
FOREIGN_ORIGIN = "https://evil.example"


def _cors_request(
    url: str,
    *,
    method: str = "GET",
    origin: str,
    token: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Send a request carrying an ``Origin`` header and keep the raw headers."""
    request = urllib.request.Request(url, method=method)
    request.add_header("Origin", origin)
    request.add_header("Access-Control-Request-Method", "POST")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


async def test_preflight_from_a_loopback_origin_needs_no_token(config: RuntimeConfig) -> None:
    """Browsers never put credentials on a preflight, so it cannot require one."""
    runtime, service = await _service(config)
    try:
        status, headers, _ = await asyncio.to_thread(
            _cors_request,
            f"{service.url}/tasks",
            method="OPTIONS",
            origin=LOOPBACK_ORIGIN,
        )
        assert status == 204
        lowered = {key.lower(): value for key, value in headers.items()}
        assert lowered["access-control-allow-origin"] == LOOPBACK_ORIGIN
        assert "authorization" in lowered["access-control-allow-headers"]
    finally:
        await service.stop()
        await runtime.close()


async def test_preflight_from_another_origin_is_refused(config: RuntimeConfig) -> None:
    runtime, service = await _service(config)
    try:
        status, headers, body = await asyncio.to_thread(
            _cors_request,
            f"{service.url}/tasks",
            method="OPTIONS",
            origin=FOREIGN_ORIGIN,
        )
        assert status == 401
        lowered = {key.lower(): value for key, value in headers.items()}
        assert "access-control-allow-origin" not in lowered
        assert json.loads(body)["error"]["code"] == "unauthorized"
    finally:
        await service.stop()
        await runtime.close()


async def test_cors_does_not_replace_the_token(config: RuntimeConfig) -> None:
    """A loopback page still gets 401 without a token; CORS only lets it read that."""
    runtime, service = await _service(config)
    try:
        status, headers, body = await asyncio.to_thread(
            _cors_request, f"{service.url}/health", origin=LOOPBACK_ORIGIN
        )
        assert status == 401
        assert json.loads(body)["error"]["code"] == "unauthorized"
        lowered = {key.lower(): value for key, value in headers.items()}
        # The header is present so the page can *see* the rejection — not to let it in.
        assert lowered["access-control-allow-origin"] == LOOPBACK_ORIGIN
    finally:
        await service.stop()
        await runtime.close()


async def test_a_foreign_origin_gets_no_response_even_with_a_valid_token(
    config: RuntimeConfig,
) -> None:
    """Withholding the header is what stops a remote page reading the body.

    The request itself succeeds — the token is what authorises it — but a browser
    will not hand the response to a document on another origin.
    """
    runtime, service = await _service(config)
    try:
        status, headers, body = await asyncio.to_thread(
            _cors_request, f"{service.url}/health", origin=FOREIGN_ORIGIN, token=TOKEN
        )
        assert status == 200
        assert json.loads(body)["state"] == "ok"
        lowered = {key.lower(): value for key, value in headers.items()}
        assert "access-control-allow-origin" not in lowered
        assert "access-control-allow-headers" not in lowered
    finally:
        await service.stop()
        await runtime.close()


async def test_a_lookalike_host_is_not_loopback(config: RuntimeConfig) -> None:
    """``localhost.evil.example`` must not pass a substring check."""
    runtime, service = await _service(config)
    try:
        for origin in ("http://localhost.evil.example", "http://127.0.0.1.evil.example"):
            status, headers, _ = await asyncio.to_thread(
                _cors_request, f"{service.url}/health", origin=origin, token=TOKEN
            )
            assert status == 200
            lowered = {key.lower(): value for key, value in headers.items()}
            assert "access-control-allow-origin" not in lowered, origin
    finally:
        await service.stop()
        await runtime.close()
