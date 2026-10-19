"""Unified error model for DeskWeave.

Every error raised by DeskWeave code should derive from :class:`DeskWeaveError`
so that callers (CLI, MCP gateway, HTTP control API, UI) can map failures to a
stable machine-readable ``code`` and a human-readable ``message`` without
inspecting exception classes across package boundaries.

Errors are *diagnostic*: they always carry a message and may carry a ``details``
mapping. Empty ``except`` blocks are forbidden by project policy; if an error is
re-raised or wrapped, the original exception is preserved via ``__cause__``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DeskWeaveError(Exception):
    """Base class for all DeskWeave errors."""

    code: str = "deskweave_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the error."""
        return {"code": self.code, "message": self.message, "details": self.details}

    def to_info(self) -> ErrorInfo:
        """Return the serialisable :class:`ErrorInfo` form of this error."""
        return ErrorInfo(code=self.code, message=self.message, details=self.details)

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            return f"[{self.code}] {self.message} ({self.details})"
        return f"[{self.code}] {self.message}"


class ErrorInfo(BaseModel):
    """Serialisable error payload shared across process boundaries."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error message.")
    details: dict[str, Any] = Field(default_factory=dict)


class ConfigError(DeskWeaveError):
    """Invalid or missing configuration."""

    code = "config_error"


class ValidationError(DeskWeaveError):
    """An argument or payload failed validation."""

    code = "validation_error"


class NotFoundError(DeskWeaveError):
    """A requested entity does not exist."""

    code = "not_found"


class ConflictError(DeskWeaveError):
    """The operation conflicts with current state (e.g. duplicate registration)."""

    code = "conflict"


class LifecycleError(DeskWeaveError):
    """An operation was attempted in an invalid lifecycle state."""

    code = "lifecycle_error"


class PersistenceError(DeskWeaveError):
    """A storage / database operation failed."""

    code = "persistence_error"


class AdapterError(DeskWeaveError):
    """Base class for adapter-related failures."""

    code = "adapter_error"


class AdapterConnectionError(AdapterError):
    """An adapter could not be (dis)connected."""

    code = "adapter_connection_error"


class AdapterExecutionError(AdapterError):
    """An adapter operation failed while executing."""

    code = "adapter_execution_error"


class AdapterTimeoutError(AdapterError):
    """An adapter operation exceeded its deadline."""

    code = "adapter_timeout_error"


class PermissionDeniedError(DeskWeaveError):
    """An operation was denied by the permission layer."""

    code = "permission_denied"


class NotImplementedYetError(DeskWeaveError):
    """Explicit marker for functionality that is planned but not implemented.

    Used instead of returning fabricated success values.
    """

    code = "not_implemented"


__all__ = [
    "AdapterConnectionError",
    "AdapterError",
    "AdapterExecutionError",
    "AdapterTimeoutError",
    "ConfigError",
    "ConflictError",
    "DeskWeaveError",
    "ErrorInfo",
    "LifecycleError",
    "NotFoundError",
    "NotImplementedYetError",
    "PermissionDeniedError",
    "PersistenceError",
    "ValidationError",
]
