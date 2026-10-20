"""Token-gated, loopback-only HTTP control API.

The desktop shell (and local tooling) talk to the runtime through this small JSON
API. It is the *only* way the webview can reach the runtime, and it never exposes
the runtime token, filesystem paths or adapter internals.

Routes
------
``GET  /health``                  runtime health
``GET  /adapters``                registered adapter descriptors
``GET  /tasks``                   task views (optionally ``?status=``)
``POST /tasks``                   start a reference collection; returns immediately
``GET  /tasks/{id}``              one task view (task + candidate artifacts)
``POST /tasks/{id}/confirm``      execute the user's selection exactly once
``POST /tasks/{id}/cancel``       cancel a pending or running task
``GET  /artifacts/{id}``          artifact metadata (no local path)
``GET  /artifacts/{id}/preview``  artifact bytes, served from the store
``POST /artifacts/{id}/open``     hand the file to the OS (``reveal`` / ``open``)
``GET  /events``                  recent events
``POST /shutdown``                graceful shutdown

Every request must carry the shared token (``Authorization: Bearer <token>`` or
``X-DeskWeave-Token``). The server binds to a loopback address only (enforced by
:class:`~deskweave_runtime.config.RuntimeConfig`).

Errors use one shape: ``{"error": {"code", "message", "details"}}`` so the UI can
branch on ``code`` without parsing prose.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError as PydanticValidationError

from deskweave_contracts import (
    CollectionRequest,
    ConfirmRequest,
    DeskWeaveError,
    ReferenceTaskView,
    TaskStatus,
    ValidationError,
)

from ..artifacts import ACTIONS
from ..logging import get_logger
from ..references import public_artifact
from ..runtime import Runtime

#: Upper bound for a request body. Every accepted body is a small JSON object.
MAX_BODY_BYTES = 64 * 1024

#: Default deadline for a control call, in seconds.
DEFAULT_REQUEST_TIMEOUT = 30.0

#: A confirmation drives Blender, which can legitimately take minutes.
CONFIRM_TIMEOUT = 240.0

_ID = r"[A-Za-z0-9_-]{1,64}"
_TASK_ROUTE = re.compile(rf"^/tasks/(?P<task_id>{_ID})(?P<action>/confirm|/cancel)?$")
_ARTIFACT_ROUTE = re.compile(rf"^/artifacts/(?P<artifact_id>{_ID})(?P<action>/preview|/open)?$")

#: Maps a stable error code to an HTTP status.
_STATUS_BY_CODE: dict[str, int] = {
    "unauthorized": 401,
    "validation_error": 400,
    "not_found": 404,
    "conflict": 409,
    "permission_denied": 403,
    "browser_access_denied": 403,
    "unsafe_resource": 422,
    "not_implemented": 501,
    "adapter_timeout_error": 504,
    "adapter_connection_error": 503,
    "adapter_execution_error": 502,
    "adapter_error": 502,
}


@dataclass(frozen=True)
class Response:
    """A control API response: JSON payload or raw bytes."""

    status: int
    payload: dict[str, Any] | None = None
    body: bytes | None = None
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> Response:
    return Response(
        status=_STATUS_BY_CODE.get(code, 500),
        payload={"error": {"code": code, "message": message, "details": details or {}}},
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "DeskWeaveRuntime/0.1.0"

    @property
    def service(self) -> RuntimeService:
        return self.server.service  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        """Answer CORS preflight for loopback origins.

        The desktop shell talks over IPC and needs none of this; a browser-based
        dev session does. Preflight requests carry no ``Authorization`` header
        (browsers omit it), so authorisation is not required here — but only
        loopback origins are answered at all.
        """
        if _cors_origin(self.headers) is None:
            self._send(_error("unauthorized", "cross-origin requests are not allowed"))
            return
        self._send(Response(204, payload={}))

    def _dispatch(self, method: str) -> None:
        if not self.service.is_authorized(self.headers):
            self._send(_error("unauthorized", "a valid runtime token is required"))
            return

        body = b""
        if method == "POST":
            body, problem = self._read_body()
            if problem is not None:
                self._send(problem)
                return
        try:
            response = self.service.handle(method, self.path, body)
        except Exception as exc:
            self.service.logger.exception("control API request failed: %s %s", method, self.path)
            response = _error("internal_error", f"{type(exc).__name__}: {exc}")
        self._send(response)

    def _read_body(self) -> tuple[bytes, Response | None]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b"", _error("validation_error", "invalid Content-Length header")
        if length < 0:
            return b"", _error("validation_error", "invalid Content-Length header")
        if length > MAX_BODY_BYTES:
            return b"", _error(
                "validation_error",
                f"request body exceeds {MAX_BODY_BYTES} bytes",
                {"limit": MAX_BODY_BYTES},
            )
        return self.rfile.read(length) if length else b"", None

    def _send(self, response: Response) -> None:
        if response.body is not None:
            body = response.body
        else:
            body = json.dumps(response.payload or {}).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = _cors_origin(self.headers)
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "origin")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Never log headers or bodies: they carry the token and user selections.
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

    def handle(self, method: str, path: str, body: bytes = b"") -> Response:
        """Route one request. Never raises ``DeskWeaveError``."""
        split = urlsplit(path)
        route = split.path.rstrip("/") or "/"
        query = parse_qs(split.query)

        try:
            return self._route(method, route, query, body)
        except DeskWeaveError as exc:
            info = exc.to_info()
            return _error(info.code, info.message, info.details)
        except PydanticValidationError as exc:
            return _error(
                "validation_error",
                "request payload failed validation",
                {"errors": _pydantic_messages(exc)},
            )

    def _route(self, method: str, route: str, query: dict[str, list[str]], body: bytes) -> Response:
        if method == "GET":
            if route == "/health":
                return Response(200, self._run(self._health()))
            if route == "/adapters":
                return Response(200, self._run(self._adapters()))
            if route == "/tasks":
                return Response(200, self._run(self._tasks(query)))
            if route == "/events":
                return Response(200, self._events())
            task_match = _TASK_ROUTE.match(route)
            if task_match and not task_match.group("action"):
                view = self._run(self._view(task_match.group("task_id")))
                return Response(200, view)
            artifact_match = _ARTIFACT_ROUTE.match(route)
            if artifact_match and artifact_match.group("action") != "/open":
                return self._artifact(artifact_match)

        if method == "POST":
            if route == "/tasks":
                return self._start_collection(body)
            if route == "/shutdown":
                self.request_shutdown()
                return Response(200, {"status": "shutting_down"})
            task_match = _TASK_ROUTE.match(route)
            if task_match:
                action = task_match.group("action")
                if action == "/confirm":
                    return self._confirm(task_match.group("task_id"), body)
                if action == "/cancel":
                    return Response(200, self._run(self._cancel(task_match.group("task_id"))))
            artifact_match = _ARTIFACT_ROUTE.match(route)
            if artifact_match and artifact_match.group("action") == "/open":
                return self._open_artifact(artifact_match.group("artifact_id"), body)

        return _error("not_found", f"no such route: {method} {route}")

    # -- endpoints --------------------------------------------------------
    def _start_collection(self, body: bytes) -> Response:
        payload = _json_object(body)
        request = CollectionRequest.model_validate(payload)
        view = self._run(self._collect(request))
        return Response(202, view, headers={"Location": f"/tasks/{view['task']['task_id']}"})

    def _confirm(self, task_id: str, body: bytes) -> Response:
        payload = _json_object(body)
        request = ConfirmRequest.model_validate(payload)
        view = self._run(self._confirm_task(task_id, request), timeout=CONFIRM_TIMEOUT)
        return Response(200, view)

    def _artifact(self, match: re.Match[str]) -> Response:
        artifact_id = match.group("artifact_id")
        if match.group("action") == "/preview":
            body, media_type = self._run(self._read_artifact(artifact_id))
            return Response(
                200,
                body=body,
                content_type=media_type,
                headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
            )
        artifact = self._run(self._runtime.artifacts.get(artifact_id))
        return Response(200, {"artifact": public_artifact(artifact)})

    def _open_artifact(self, artifact_id: str, body: bytes) -> Response:
        """Hand a verified artifact file to the OS (user-initiated).

        The response never contains the path: the webview asked for an id and an
        action, and gets an acknowledgement, not a filesystem location.
        """
        payload = _json_object(body)
        action = payload.get("action")
        if not isinstance(action, str):
            raise ValidationError(
                "an artifact action is required", details={"allowed": sorted(ACTIONS)}
            )
        opened = self._run(self._runtime.opener.open(artifact_id, action))
        return Response(200, {"artifact": opened})

    async def _collect(self, request: CollectionRequest) -> dict[str, Any]:
        task = await self._runtime.references.start_collection(request)
        return {"task": task.model_dump(mode="json"), "artifacts": []}

    async def _confirm_task(self, task_id: str, request: ConfirmRequest) -> dict[str, Any]:
        await self._runtime.references.confirm(task_id, request)
        return await self._view(task_id)

    async def _cancel(self, task_id: str) -> dict[str, Any]:
        await self._runtime.references.cancel(task_id)
        return await self._view(task_id)

    async def _view(self, task_id: str) -> dict[str, Any]:
        view: ReferenceTaskView = await self._runtime.references.get_view(task_id)
        return _serialise_view(view)

    async def _tasks(self, query: dict[str, list[str]]) -> dict[str, Any]:
        raw_status = (query.get("status") or [None])[0]
        status: TaskStatus | None = None
        if raw_status:
            try:
                status = TaskStatus(raw_status)
            except ValueError as exc:
                raise ValidationError(
                    f"unknown task status {raw_status!r}",
                    details={"allowed": [item.value for item in TaskStatus]},
                ) from exc
        views = await self._runtime.references.list_views(status=status)
        return {"tasks": [_serialise_view(view) for view in views]}

    async def _health(self) -> dict[str, Any]:
        health = await self._runtime.health()
        return health.model_dump(mode="json")

    async def _adapters(self) -> dict[str, Any]:
        descriptors = await self._runtime.list_adapters()
        return {"adapters": [descriptor.model_dump(mode="json") for descriptor in descriptors]}

    async def _read_artifact(self, artifact_id: str) -> tuple[bytes, str]:
        return await self._runtime.artifacts.read_bytes(artifact_id)

    def _events(self) -> dict[str, Any]:
        events = self._runtime.events.history()
        return {"events": [event.model_dump(mode="json") for event in events]}

    # -- plumbing ---------------------------------------------------------
    def _run(self, coro: Any, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Any:
        if self._loop is None:
            raise RuntimeError("service is not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise ValidationError(
                f"runtime did not answer within {timeout:.0f}s",
                details={"timeout_seconds": timeout},
            ) from None


def _serialise_view(view: ReferenceTaskView) -> dict[str, Any]:
    return {
        "task": view.task.model_dump(mode="json"),
        "artifacts": [public_artifact(artifact) for artifact in view.artifacts],
    }


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        raise ValidationError("a JSON body is required")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")
    return payload


def _pydantic_messages(exc: PydanticValidationError) -> list[str]:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        messages.append(f"{location}: {error['msg']}" if location else error["msg"])
    return messages


#: Hosts that count as "this machine". Anything else gets no CORS headers at all.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _cors_origin(headers: Any) -> str | None:
    """Return the request origin when it is a loopback page, else ``None``.

    Loopback-only is deliberate: a remote page must not be able to read runtime
    responses even if it somehow obtained the token, and the token is still
    required for every actual request.
    """
    origin = headers.get("Origin")
    if not origin:
        return None
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"}:
        return None
    if (parts.hostname or "") not in _LOOPBACK_HOSTS:
        return None
    return origin


__all__ = ["CONFIRM_TIMEOUT", "MAX_BODY_BYTES", "Response", "RuntimeService"]
