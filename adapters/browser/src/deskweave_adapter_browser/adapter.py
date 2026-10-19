"""Browser connector (Phase 0 skeleton).

The lifecycle and capability declarations are real; the actual Playwright-driven
automation is deliberately **not** implemented yet. ``connect`` verifies that the
Playwright dependency is importable and reports an honest error if it is not, and
``_run`` raises :class:`NotImplementedYetError` instead of fabricating success.
"""

from __future__ import annotations

from importlib.util import find_spec

from deskweave_adapter_sdk import Adapter, PermissionChecker
from deskweave_contracts import (
    AdapterConnectionError,
    AdapterDescriptor,
    AdapterStatus,
    Capability,
    NotImplementedYetError,
    OperationRequest,
    PermissionScope,
    RiskLevel,
)

BROWSER_CAPABILITIES: list[Capability] = [
    Capability(
        name="open_page",
        description="Open a URL in a controlled browser session.",
        parameters_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MODERATE,
        scope=PermissionScope.BROWSER_CONTROL,
        requires_confirmation=True,
    ),
    Capability(
        name="collect_reference_images",
        description="Search and collect candidate reference images for a query.",
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MODERATE,
        scope=PermissionScope.BROWSER_CONTROL,
        requires_confirmation=True,
    ),
]


class BrowserAdapter(Adapter):
    """Connector to a controlled browser session."""

    def __init__(
        self, *, adapter_id: str = "browser", permission_checker: PermissionChecker | None = None
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Browser",
            version="0.0.1",
            description="Browser connector (Playwright-based). Phase 0 skeleton.",
            capabilities=BROWSER_CAPABILITIES,
        )
        super().__init__(descriptor, permission_checker=permission_checker)

    async def connect(self) -> None:
        if find_spec("playwright") is None:
            self._set_status(AdapterStatus.ERROR)
            raise AdapterConnectionError(
                "playwright is not installed; the browser connector is unavailable",
                details={"adapter_id": self.adapter_id, "hint": "install playwright to enable it"},
            )
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        self._set_status(AdapterStatus.DISCONNECTED)

    async def _run(self, request: OperationRequest) -> dict:
        raise NotImplementedYetError(
            f"browser capability {request.capability!r} is not implemented in Phase 0",
            details={"adapter_id": self.adapter_id, "capability": request.capability},
        )


__all__ = ["BROWSER_CAPABILITIES", "BrowserAdapter"]
