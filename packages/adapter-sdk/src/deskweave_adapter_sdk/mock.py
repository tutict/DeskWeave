"""A fully working in-memory adapter used by contract and integration tests.

``MockAdapter`` is intentionally part of the shipped SDK (not test-only) so that
downstream components (runtime, MCP gateway, desktop UI) can be exercised without
real desktop applications.
"""

from __future__ import annotations

import asyncio
from typing import Any

from deskweave_contracts import (
    AdapterConnectionError,
    AdapterDescriptor,
    AdapterExecutionError,
    AdapterStatus,
    Capability,
    OperationRequest,
    RiskLevel,
)

from .base import Adapter, PermissionChecker

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


def default_capabilities() -> list[Capability]:
    """Capabilities offered by a default :class:`MockAdapter`."""
    return [
        Capability(
            name="echo",
            description="Return the provided message unchanged.",
            parameters_schema=ECHO_SCHEMA,
            risk=RiskLevel.READ_ONLY,
        ),
        Capability(
            name="slow",
            description="Sleep for the requested number of milliseconds.",
            parameters_schema={
                "type": "object",
                "properties": {"milliseconds": {"type": "integer", "minimum": 0}},
                "required": ["milliseconds"],
                "additionalProperties": False,
            },
            risk=RiskLevel.READ_ONLY,
        ),
    ]


class MockAdapter(Adapter):
    """An adapter that needs no external application.

    Parameters
    ----------
    adapter_id:
        Identifier used for registration.
    capabilities:
        Capabilities to expose; defaults to :func:`default_capabilities`.
    connect_error:
        If set, :meth:`connect` raises :class:`AdapterConnectionError`.
    run_error:
        If set, ``_run`` raises :class:`AdapterExecutionError`.
    """

    def __init__(
        self,
        adapter_id: str = "mock",
        *,
        capabilities: list[Capability] | None = None,
        connect_error: str | None = None,
        run_error: str | None = None,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Mock Adapter",
            version="0.0.1",
            description="In-memory adapter used for tests and demos.",
            capabilities=capabilities if capabilities is not None else default_capabilities(),
        )
        super().__init__(descriptor, permission_checker=permission_checker)
        self._connect_error = connect_error
        self._run_error = run_error
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_error is not None:
            self._set_status(AdapterStatus.ERROR)
            raise AdapterConnectionError(
                self._connect_error, details={"adapter_id": self.adapter_id}
            )
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._set_status(AdapterStatus.DISCONNECTED)

    async def _run(self, request: OperationRequest) -> dict[str, Any]:
        if self._run_error is not None:
            raise AdapterExecutionError(self._run_error, details={"adapter_id": self.adapter_id})
        if request.capability == "echo":
            return {"echo": request.arguments["message"]}
        if request.capability == "slow":
            await asyncio.sleep(request.arguments["milliseconds"] / 1000.0)
            return {"slept_ms": request.arguments["milliseconds"]}
        return {"capability": request.capability, "arguments": dict(request.arguments)}


__all__ = ["ECHO_SCHEMA", "MockAdapter", "default_capabilities"]
