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
from collections.abc import Iterable
from datetime import UTC, datetime

from deskweave_contracts import (
    DecisionVia,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    PermissionScope,
    RiskLevel,
    new_id,
)

from ..persistence import PermissionGrantRepository


def grant_is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """Would this grant row be refused for being past its expiry?

    One predicate, used by ``check`` and by ``list_effective_grants``. Phase 2.6.2
    had two: ``check`` filtered on expiry, ``list_grants`` did not, so inspection
    and approval reconciliation both counted an expired row as authorisation that
    dispatch would refuse.

    A missing expiry means "no expiry". A value that cannot be parsed counts as
    expired: the alternative is that a typo becomes a permanent grant, and this
    module's job is to fail closed. A naive timestamp is read as UTC rather than
    raising — same decision, without turning a bad row into an exception on the
    hot path.
    """
    if expires_at is None:
        return False
    try:
        when = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when < (now or datetime.now(UTC))


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
        #: One-shot allowances that a ``check`` consumed and that are waiting to
        #: learn whether the call they authorised actually started. Keeping them
        #: here is what makes :meth:`conclude` able to hand one back: a
        #: confirmation that bought nothing must not be spent.
        self._pending: set[tuple[str, str]] = set()

    async def check(self, request: PermissionRequest) -> PermissionResult:
        if request.scope is PermissionScope.CODE_EXECUTION:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="code execution is not permitted",
                via=DecisionVia.DENY,
            )

        key = (request.adapter_id, request.capability)
        if key in self._one_shot:
            # Consumed atomically with the test: there is no await between the
            # two, so two concurrent checks cannot both spend the same
            # confirmation. The entry is parked in ``_pending`` rather than
            # dropped, because at this point we still do not know whether the
            # call will be admitted.
            self._one_shot.discard(key)
            self._pending.add(key)
            self._logger.info(
                "one-shot permission consumed for %s/%s", request.adapter_id, request.capability
            )
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="confirmed by the user for this operation",
                via=DecisionVia.ONE_SHOT,
            )

        grant = await self._find_grant(request.adapter_id, request.capability)
        if grant is not None:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="explicit permission grant",
                grant_id=grant["grant_id"],
                via=DecisionVia.GRANT,
            )

        if request.risk is RiskLevel.READ_ONLY:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="read-only operation",
                via=DecisionVia.POLICY,
            )

        if request.scope is PermissionScope.BROWSER_CONTROL and not request.requires_confirmation:
            # Public web reads with no writes outside the artifact sandbox. The
            # browser is still sandboxed by the artifact store's path policy.
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="browser collection without confirmation requirement",
                via=DecisionVia.POLICY,
            )

        return PermissionResult(
            decision=PermissionDecision.REQUIRE_CONFIRMATION,
            via=DecisionVia.CONFIRMATION,
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

    @property
    def outstanding_one_shots(self) -> frozenset[tuple[str, str]]:
        """Confirmations not yet spent. Read-only view for callers and tests."""
        return frozenset(self._one_shot)

    @property
    def reserved_one_shots(self) -> frozenset[tuple[str, str]]:
        """Confirmations consumed by ``check`` awaiting a disposition.

        Non-empty means a call was authorised and has not yet said whether it
        started. A reservation that never leaves this set is a confirmation the
        user gave and nothing ever used — see ``conclude``.
        """
        return frozenset(self._pending)

    def conclude(self, request: PermissionRequest, *, started: bool) -> bool:
        """Settle a one-shot that :meth:`check` consumed. Returns ``True`` if it was handed back.

        ``started=False`` means the call was refused before it began — cost,
        concurrency, rate, shutdown or revocation. In that case the confirmation
        has bought nothing and is put back, because the alternative is telling a
        user "you said yes once" about a call that never happened.

        ``started=True`` drops the reservation: the allowance is spent, whether
        the capability succeeded, failed, timed out or was cancelled. A timeout
        is *not* a reason to hand it back — the work may well have run.

        Callers that do not know whether a call started should pass
        ``started=True``: forgetting a reservation can only ever be more strict
        than the truth, which is the direction that matters here.
        """
        key = (request.adapter_id, request.capability)
        if key not in self._pending:
            return False
        self._pending.discard(key)
        if started:
            return False
        self._one_shot.add(key)
        self._logger.info(
            "one-shot permission returned for %s/%s: the call never started",
            request.adapter_id,
            request.capability,
        )
        return True

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
        """Every grant row, expired or not: the audit view.

        Deliberately unfiltered. "This grant existed and has since expired" is
        history an operator needs, and hiding it here would delete the only record
        that it was ever issued. Callers asking "what would authorise a call now"
        want :meth:`list_effective_grants` instead.
        """
        if self._grants is None:
            return []
        return await self._grants.list()

    async def list_effective_grants(self) -> list[dict]:
        """Grant rows that would still authorise a call now.

        Same rows as :meth:`list_grants`, minus those ``grant_is_expired`` — the
        predicate ``check`` applies. Anything that reconciles a decision against
        "what dispatch will honour" must read this, not the raw list.
        """
        return [
            row for row in await self.list_grants() if not grant_is_expired(row.get("expires_at"))
        ]

    async def revoke(self, adapter_id: str, capabilities: Iterable[str] | None = None) -> list[str]:
        """Remove grants for ``adapter_id`` and return the ids that existed.

        Revocation has to cover every place an allowance can be remembered, not
        just the database: a pending ``allow_once`` entry lives in memory and would
        otherwise authorise exactly one more call *after* the user revoked
        everything. Both are cleared here, in one method, so a caller cannot
        revoke half of the state.

        The check path reads the database on every call and keeps no grant cache,
        which is what makes this take effect on the next call rather than at the
        next restart. An operation that already passed the check is *not*
        interrupted — see ``docs/architecture/plugin-lifecycle.md`` §5.
        """
        # One predicate, used twice: a key belongs to this revocation when it is
        # for this principal and (no capability filter was given, or its
        # capability is one of the revoked ones). Computed once into a frozenset
        # rather than consulted twice: ``capabilities`` is an ``Iterable``, and a
        # generator handed to both ``set(...)`` and the repository would be empty
        # by its second read — which deletes nothing and still reports success.
        wanted = None if capabilities is None else frozenset(capabilities)

        def covered(key: tuple[str, str]) -> bool:
            return key[0] == adapter_id and (wanted is None or key[1] in wanted)

        dropped: list[tuple[str, str]] = [key for key in self._one_shot if covered(key)]
        for key in dropped:
            self._one_shot.discard(key)
        # A reservation is an allowance that a check already spent. Leaving one
        # behind would let ``conclude(started=False)`` put it back *after* the
        # revocation, which is exactly the "one more call after you said stop"
        # hole this method exists to close.
        self._pending = {key for key in self._pending if not covered(key)}
        if self._grants is None:
            reason = "no grant repository configured"
            removed: list[str] = []
        else:
            removed = await self._grants.delete(
                adapter_id, None if wanted is None else tuple(wanted)
            )
            reason = f"{len(removed)} grant(s)"
        self._logger.info(
            "revoked %s for %s (%d one-shot allowance(s) dropped)",
            reason,
            adapter_id,
            len(dropped),
        )
        return removed

    async def _find_grant(self, adapter_id: str, capability: str) -> dict | None:
        if self._grants is None:
            return None
        grant = await self._grants.find(adapter_id, capability)
        if grant is None:
            return None
        if grant_is_expired(grant.get("expires_at")):
            return None
        return grant


__all__ = ["PermissionManager", "grant_is_expired"]
