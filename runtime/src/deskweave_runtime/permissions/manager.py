"""The single permission layer shared by every adapter.

Policy (Phase 0, deliberately conservative):

* ``code:execution`` scope is always denied — there is no generic code-run entry
  point in this phase.
* an explicit, unexpired grant for ``(adapter_id, capability)`` allows the call;
* otherwise read-only capabilities are allowed;
* everything else requires user confirmation (reported as a decision, not
  silently allowed).

Adapters never implement their own permission logic; they call
:meth:`PermissionManager.check` through the SDK's ``execute`` template.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from deskweave_contracts import (
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    PermissionScope,
    RiskLevel,
    new_id,
)

from ..persistence import PermissionGrantRepository


class PermissionManager:
    """Evaluates :class:`PermissionRequest` objects against the local policy."""

    def __init__(
        self,
        *,
        grant_repository: PermissionGrantRepository | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._grants = grant_repository
        self._logger = logger or logging.getLogger("deskweave.permissions")

    async def check(self, request: PermissionRequest) -> PermissionResult:
        if request.scope is PermissionScope.CODE_EXECUTION:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="code execution is not permitted in this phase",
            )

        grant = await self._find_grant(request.adapter_id, request.capability)
        if grant is not None:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="explicit permission grant",
                grant_id=grant["grant_id"],
            )

        if request.risk is RiskLevel.READ_ONLY:
            return PermissionResult(decision=PermissionDecision.ALLOW, reason="read-only operation")

        return PermissionResult(
            decision=PermissionDecision.REQUIRE_CONFIRMATION,
            reason=f"risk level {request.risk.value!r} requires user confirmation",
        )

    async def grant(
        self,
        request: PermissionRequest,
        *,
        granted_by: str = "user",
        expires_at: datetime | None = None,
    ) -> str:
        """Record an explicit grant and return its id."""
        grant_id = new_id("grant")
        if self._grants is None:
            raise RuntimeError("no grant repository configured")
        await self._grants.create(
            grant_id=grant_id,
            adapter_id=request.adapter_id,
            capability=request.capability,
            scope=request.scope.value,
            risk=request.risk.value,
            granted_by=granted_by,
            expires_at=expires_at.isoformat() if expires_at is not None else None,
        )
        self._logger.info("granted %s/%s to %s", request.adapter_id, request.capability, granted_by)
        return grant_id

    async def list_grants(self) -> list[dict]:
        if self._grants is None:
            return []
        return await self._grants.list()

    async def _find_grant(self, adapter_id: str, capability: str) -> dict | None:
        if self._grants is None:
            return None
        grant = await self._grants.find(adapter_id, capability)
        if grant is None:
            return None
        expires_at = grant.get("expires_at")
        if expires_at is not None and datetime.fromisoformat(expires_at) < datetime.now(UTC):
            return None
        return grant


__all__ = ["PermissionManager"]
