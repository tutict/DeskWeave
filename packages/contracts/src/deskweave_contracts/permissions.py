"""Permission model contracts.

DeskWeave is local-first but can drive powerful local capabilities (file writes,
external process launches, code execution). This module defines the *data* model
for permissions. The *enforcement* logic lives in the runtime
(``deskweave_runtime.permissions``) so that a single, shared permission system is
used everywhere instead of one per adapter.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(StrEnum):
    """How dangerous an operation is, used to decide the required consent."""

    READ_ONLY = "read_only"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class PermissionScope(StrEnum):
    """The kind of resource an operation touches."""

    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    PROCESS_SPAWN = "process:spawn"
    NETWORK = "network"
    BROWSER_CONTROL = "browser:control"
    BLENDER_CONTROL = "blender:control"
    CODE_EXECUTION = "code:execution"


class PermissionRequest(BaseModel):
    """A request to perform an operation, subject to a permission check."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: str = Field(description="Adapter requesting the operation.")
    capability: str = Field(description="Capability being invoked.")
    scope: PermissionScope = Field(description="Resource scope touched.")
    risk: RiskLevel = Field(default=RiskLevel.READ_ONLY)
    requires_confirmation: bool = Field(
        default=False,
        description="The capability declares that a human must confirm it.",
    )
    resource: str | None = Field(
        default=None,
        description="Logical resource identifier (NOT a filesystem path used as a token).",
    )
    reason: str | None = Field(default=None, description="Why the operation is requested.")


class PermissionDecision(StrEnum):
    """Outcome of a permission check."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class PermissionResult(BaseModel):
    """Result of evaluating a :class:`PermissionRequest`."""

    model_config = ConfigDict(extra="forbid")

    decision: PermissionDecision
    reason: str = ""
    grant_id: str | None = None


__all__ = [
    "PermissionDecision",
    "PermissionRequest",
    "PermissionResult",
    "PermissionScope",
    "RiskLevel",
]
