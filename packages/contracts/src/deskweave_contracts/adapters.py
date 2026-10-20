"""Adapter data contracts.

These models describe *what* an adapter is and *how* operations are requested and
reported. The behavioural protocol (lifecycle methods) lives in
``deskweave_adapter_sdk``; keeping the data model here lets the runtime, the MCP
gateway and the desktop UI share it without depending on the SDK implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorInfo
from .permissions import PermissionScope, RiskLevel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AdapterStatus(StrEnum):
    """Lifecycle status of an adapter instance."""

    UNKNOWN = "unknown"
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class Capability(BaseModel):
    """A single operation an adapter can perform."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Stable capability name, unique within an adapter.")
    description: str = ""
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing accepted arguments.",
    )
    risk: RiskLevel = RiskLevel.READ_ONLY
    scope: PermissionScope = PermissionScope.FILESYSTEM_READ
    requires_confirmation: bool = False


class AdapterDescriptor(BaseModel):
    """Static description of an adapter, safe to expose to agents and the UI."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    name: str
    version: str = "0.0.0"
    description: str = ""
    capabilities: list[Capability] = Field(default_factory=list)


class OperationRequest(BaseModel):
    """A request to execute one capability on an adapter."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    reason: str | None = None
    task_id: str | None = Field(
        default=None,
        description="Workflow task this execution belongs to, if any (audit + authorisation).",
    )


class OperationResult(BaseModel):
    """Outcome of an :class:`OperationRequest`.

    ``ok`` is the single source of truth for success; ``error`` is populated only
    when ``ok`` is ``False``. This avoids fabricated success values.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    capability: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime = Field(default_factory=_utcnow)

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000.0


__all__ = [
    "AdapterDescriptor",
    "AdapterStatus",
    "Capability",
    "OperationRequest",
    "OperationResult",
]
