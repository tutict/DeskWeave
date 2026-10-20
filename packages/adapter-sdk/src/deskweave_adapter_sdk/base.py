"""Adapter contract and lifecycle.

An :class:`Adapter` is a connector to a desktop application (browser, Blender,
filesystem, ...). The base class implements a *template* ``execute`` method so
that every connector gets the same guarantees:

1. the requested capability must exist;
2. arguments are validated against the capability's JSON Schema;
3. a permission check runs before any side effect (extension point);
4. the raw work runs in ``_run`` and any failure is reported as a structured,
   non-silent :class:`~deskweave_contracts.OperationResult` with ``ok=False``.

Adapters implement ``_run`` (plus ``connect`` / ``get_capabilities`` /
``disconnect``). They must never expose a generic "run arbitrary code" entry
point.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from deskweave_contracts import (
    AdapterDescriptor,
    AdapterExecutionError,
    AdapterStatus,
    AdapterTimeoutError,
    Capability,
    DeskWeaveError,
    OperationRequest,
    OperationResult,
    PermissionDecision,
    PermissionDeniedError,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
    ValidationError,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class PermissionChecker(Protocol):
    """Extension point used by :meth:`Adapter.execute` before side effects."""

    async def check(self, request: PermissionRequest) -> PermissionResult: ...


#: Maps a logical artifact id to a verified local path. Wired by the runtime to
#: the artifact store so adapters never accept filesystem paths from callers.
ResourceResolver = Callable[[str], Awaitable[Path]]


def validate_arguments(capability: Capability, arguments: dict[str, Any]) -> None:
    """Validate ``arguments`` against ``capability.parameters_schema``.

    Raises :class:`~deskweave_contracts.ValidationError` on failure. An empty
    schema means "no declared constraints" and accepts any object.
    """
    schema = capability.parameters_schema
    if not schema:
        return
    try:
        Draft202012Validator(schema).validate(arguments)
    except JsonSchemaValidationError as exc:
        raise ValidationError(
            f"invalid arguments for capability {capability.name!r}: {exc.message}",
            details={
                "capability": capability.name,
                "path": list(exc.absolute_path),
            },
        ) from exc


class Adapter(abc.ABC):
    """Base class for DeskWeave application connectors."""

    def __init__(
        self,
        descriptor: AdapterDescriptor,
        *,
        permission_checker: PermissionChecker | None = None,
        resource_resolver: ResourceResolver | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._permission_checker = permission_checker
        self._resource_resolver = resource_resolver
        self._logger = logger or logging.getLogger(f"deskweave.adapter.{descriptor.adapter_id}")
        self._status: AdapterStatus = AdapterStatus.DISCOVERED

    # -- identity ---------------------------------------------------------
    @property
    def adapter_id(self) -> str:
        return self._descriptor.adapter_id

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def set_permission_checker(self, checker: PermissionChecker | None) -> None:
        """Wire (or clear) the permission checker used by :meth:`execute`."""
        self._permission_checker = checker

    def set_resource_resolver(self, resolver: ResourceResolver | None) -> None:
        """Wire (or clear) the artifact-id resolver used by :meth:`resolve_artifact`."""
        self._resource_resolver = resolver

    async def resolve_artifact(self, artifact_id: str) -> Path:
        """Resolve an authorized artifact id to a local path.

        Adapters must use this instead of accepting paths from callers: the
        runtime re-checks that the artifact exists and lives inside an allowed
        root, so a tool argument can never point at ``C:\\Windows\\...``.
        """
        if self._resource_resolver is None:
            raise PermissionDeniedError(
                f"adapter {self.adapter_id!r} has no artifact resolver; "
                "local files cannot be addressed",
                details={"adapter_id": self.adapter_id, "artifact_id": artifact_id},
            )
        return await self._resource_resolver(artifact_id)

    # -- lifecycle --------------------------------------------------------
    async def discover(self) -> AdapterDescriptor:
        """Return the static descriptor. Cheap and side-effect free."""
        return self._descriptor

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the connection to the target application."""

    @abc.abstractmethod
    async def get_capabilities(self) -> list[Capability]:
        """Return the capabilities currently offered by this adapter."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Release all resources. Must be safe to call repeatedly."""

    async def get_status(self) -> AdapterStatus:
        """Return the current lifecycle status."""
        return self._status

    def _set_status(self, status: AdapterStatus) -> None:
        self._status = status

    # -- execution --------------------------------------------------------
    @abc.abstractmethod
    async def _run(self, request: OperationRequest) -> dict[str, Any]:
        """Perform the raw work for a validated, authorised request.

        Implementations should raise a :class:`DeskWeaveError` subclass (or any
        exception) on failure and return a JSON-serialisable mapping on success.
        """

    async def execute(self, request: OperationRequest) -> OperationResult:
        """Validate, authorise and run ``request``.

        Returns an :class:`OperationResult`; expected failures (unknown
        capability, invalid arguments, permission denial, adapter errors,
        timeouts) are reported with ``ok=False`` and a structured error rather
        than being raised. Unexpected exceptions are logged and wrapped.
        """
        started = _utcnow()
        try:
            capability = await self._resolve_capability(request.capability)
            validate_arguments(capability, request.arguments)
            await self._authorize(capability, request)
        except DeskWeaveError as exc:
            return self._failure(request, exc, started)

        try:
            if request.timeout_seconds is not None:
                data = await asyncio.wait_for(self._run(request), timeout=request.timeout_seconds)
            else:
                data = await self._run(request)
        except TimeoutError as exc:
            return self._failure(
                request,
                AdapterTimeoutError(
                    f"capability {request.capability!r} timed out after {request.timeout_seconds}s",
                    details={"adapter_id": self.adapter_id},
                ),
                started,
                cause=exc,
            )
        except DeskWeaveError as exc:
            self._logger.warning(
                "adapter %s capability %s failed: %s", self.adapter_id, request.capability, exc
            )
            return self._failure(request, exc, started)
        except Exception as exc:
            wrapped = AdapterExecutionError(
                f"adapter {self.adapter_id!r} raised {type(exc).__name__}: {exc}",
                details={"adapter_id": self.adapter_id, "type": type(exc).__name__},
            )
            self._logger.exception(
                "adapter %s capability %s raised", self.adapter_id, request.capability
            )
            return self._failure(request, wrapped, started, cause=exc)

        return OperationResult(
            ok=True,
            capability=request.capability,
            data=data,
            started_at=started,
            finished_at=_utcnow(),
        )

    # -- internals --------------------------------------------------------
    async def _resolve_capability(self, name: str) -> Capability:
        for capability in await self.get_capabilities():
            if capability.name == name:
                return capability
        raise ValidationError(
            f"unknown capability {name!r} for adapter {self.adapter_id!r}",
            details={"adapter_id": self.adapter_id, "capability": name},
        )

    async def _authorize(self, capability: Capability, request: OperationRequest) -> None:
        if self._permission_checker is None:
            # No checker wired: default to deny anything that is not a plain
            # read-only capability. This prevents an accidental "allow all".
            if capability.risk is not RiskLevel.READ_ONLY or capability.requires_confirmation:
                raise PermissionDeniedError(
                    f"capability {capability.name!r} requires a permission checker",
                    details={"adapter_id": self.adapter_id, "capability": capability.name},
                )
            return

        result = await self._permission_checker.check(
            PermissionRequest(
                adapter_id=self.adapter_id,
                capability=capability.name,
                scope=capability.scope,
                risk=capability.risk,
                requires_confirmation=capability.requires_confirmation,
                reason=request.reason,
                resource=request.task_id,
            )
        )
        if result.decision is PermissionDecision.DENY:
            raise PermissionDeniedError(
                result.reason or f"capability {capability.name!r} denied",
                details={"adapter_id": self.adapter_id, "capability": capability.name},
            )
        if result.decision is PermissionDecision.REQUIRE_CONFIRMATION:
            raise PermissionDeniedError(
                result.reason or f"capability {capability.name!r} requires user confirmation",
                details={
                    "adapter_id": self.adapter_id,
                    "capability": capability.name,
                    "decision": PermissionDecision.REQUIRE_CONFIRMATION.value,
                },
            )

    def _failure(
        self,
        request: OperationRequest,
        error: DeskWeaveError,
        started: datetime,
        *,
        cause: BaseException | None = None,
    ) -> OperationResult:
        if cause is not None:
            error.__cause__ = cause
        return OperationResult(
            ok=False,
            capability=request.capability,
            error=error.to_info(),
            started_at=started,
            finished_at=_utcnow(),
        )


__all__ = ["Adapter", "PermissionChecker", "ResourceResolver", "validate_arguments"]
