"""The single permission layer shared by every adapter.

Policy (Phase 1, deliberately conservative):

* ``code:execution`` scope is always denied — there is no generic code-run entry
  point.
* a **one-shot** allowance (recorded when the user confirms a specific operation
  in the desktop UI) permits exactly the next matching call and is then consumed;
* an explicit, unexpired grant for ``(adapter_id, capability)`` allows the call;
* read-only capabilities are allowed;
* browser collection that does not declare confirmation is allowed: it reads
  public pages and writes only into the runtime's sandboxed artifact directory;
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
        self._one_shot: set[tuple[str, str]] = set()

    async def check(self, request: PermissionRequest) -> PermissionResult:
        if request.scope is PermissionScope.CODE_EXECUTION:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="code execution is not permitted",
            )

        key = (request.adapter_id, request.capability)
        if key in self._one_shot:
            self._one_shot.discard(key)
            self._logger.info(
                "one-shot permission consumed for %s/%s", request.adapter_id, request.capability
            )
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="confirmed by the user for this operation",
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

        if request.scope is PermissionScope.BROWSER_CONTROL and not request.requires_confirmation:
            # Public web reads with no writes outside the artifact sandbox. The
            # browser is still sandboxed by the artifact store's path policy.
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="browser collection without confirmation requirement",
            )

        return PermissionResult(
            decision=PermissionDecision.REQUIRE_CONFIRMATION,
            reason=f"risk level {request.risk.value!r} requires user confirmation",
        )

    def allow_once(self, adapter_id: str, capability: str) -> None:
        """Authorise exactly one upcoming ``(adapter_id, capability)`` execution.

        Used after the user confirms a specific task in the desktop UI. The
        allowance is consumed by the next matching check, so a retry or a stale
        request cannot reuse it.
        """
        self._one_shot.add((adapter_id, capability))
        self._logger.info("one-shot permission recorded for %s/%s", adapter_id, capability)

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
