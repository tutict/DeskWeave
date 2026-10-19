"""Token-gated, loopback-only HTTP control API.

The desktop shell (and local tooling) talk to the runtime through this small JSON
API. It is deliberately minimal and read-mostly in Phase 0:

* ``GET  /health``   -> runtime health
* ``GET  /adapters`` -> registered adapter descriptors
* ``GET  /tasks``    -> task list
* ``GET  /events``   -> recent events
* ``POST /shutdown`` -> request a graceful shutdown

Every request must carry the shared token (``Authorization: Bearer <token>`` or
``X-DeskWeave-Token``). The server binds to a loopback address only (enforced by
:class:`~deskweave_runtime.config.RuntimeConfig`).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..logging import get_logger
from ..runtime import Runtime


class _Handler(BaseHTTPRequestHandler):
    server_version = "DeskWeaveRuntime/0.0.1"

    @property
    def service(self) -> RuntimeService:
        return self.server.service  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self.service.is_authorized(self.headers):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            status, payload = self.service.handle(method, self.path)
        except Exception as exc:
            self.service.logger.exception("control API request failed: %s %s", method, self.path)
            self._send(500, {"error": "internal_error", "detail": str(exc)})
            return
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        self.service.logger.debug("%s - %s", self.address_string(), format % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RuntimeService:
    """Runs the control API on a background thread against the async runtime."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        token: str,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        if not token:
            raise ValueError("a non-empty token is required")
        self._runtime = runtime
        self._token = token
        self._host = host or runtime.config.host
        self._port = port if port is not None else runtime.config.port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = asyncio.Event()
        self.logger: logging.Logger = get_logger("service")

    @property
    def port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        server = _Server((self._host, self._port), _Handler)
        server.service = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="deskweave-control-api", daemon=True
        )
        self._thread.start()
        self.logger.info("control API listening on %s", self.url)

    async def stop(self) -> None:
        if self._server is None:
            return
        await asyncio.to_thread(self._server.shutdown)
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.logger.info("control API stopped")

    async def wait_shutdown(self) -> None:
        await self._shutdown.wait()

    def request_shutdown(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)

    # -- request handling (called from the server thread) -----------------
    def is_authorized(self, headers: Any) -> bool:
        header = headers.get("Authorization", "")
        if header.startswith("Bearer "):
            provided = header[len("Bearer ") :]
        else:
            provided = headers.get("X-DeskWeave-Token", "")
        return hmac.compare_digest(provided, self._token)

    def handle(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        route = path.split("?", 1)[0].rstrip("/") or "/"
        if method == "GET" and route == "/health":
            return 200, self._run(self._health())
        if method == "GET" and route == "/adapters":
            return 200, self._run(self._adapters())
        if method == "GET" and route == "/tasks":
            return 200, self._run(self._tasks())
        if method == "GET" and route == "/events":
            return 200, self._events()
        if method == "POST" and route == "/shutdown":
            self.request_shutdown()
            return 200, {"status": "shutting_down"}
        return 404, {"error": "not_found", "path": path}

    def _run(self, coro: Any) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("service is not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    async def _health(self) -> dict[str, Any]:
        health = await self._runtime.health()
        return health.model_dump(mode="json")

    async def _adapters(self) -> dict[str, Any]:
        descriptors = await self._runtime.list_adapters()
        return {"adapters": [descriptor.model_dump(mode="json") for descriptor in descriptors]}

    async def _tasks(self) -> dict[str, Any]:
        tasks = await self._runtime.workflow.list_tasks()
        return {"tasks": [task.model_dump(mode="json") for task in tasks]}

    def _events(self) -> dict[str, Any]:
        events = self._runtime.events.history()
        return {"events": [event.model_dump(mode="json") for event in events]}


__all__ = ["RuntimeService"]
