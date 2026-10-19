"""Integration test: the local control API over a real socket."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from deskweave_adapter_sdk import MockAdapter
from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.service import RuntimeService

TOKEN = "test-token"


def _request(url: str, token: str | None, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
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
