"""Unit tests for the permission layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deskweave_contracts import (
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
    RiskLevel,
)
from deskweave_runtime import Runtime
from deskweave_runtime.permissions.manager import grant_is_expired


def _request(**overrides: object) -> PermissionRequest:
    base: dict[str, object] = {
        "adapter_id": "a",
        "capability": "c",
        "scope": PermissionScope.FILESYSTEM_READ,
        "risk": RiskLevel.READ_ONLY,
    }
    base.update(overrides)
    return PermissionRequest(**base)  # type: ignore[arg-type]


async def test_read_only_is_allowed(runtime: Runtime) -> None:
    result = await runtime.permissions.check(_request())
    assert result.decision is PermissionDecision.ALLOW


async def test_elevated_risk_requires_confirmation(runtime: Runtime) -> None:
    result = await runtime.permissions.check(_request(risk=RiskLevel.MODERATE))
    assert result.decision is PermissionDecision.REQUIRE_CONFIRMATION


async def test_code_execution_is_denied(runtime: Runtime) -> None:
    result = await runtime.permissions.check(
        _request(scope=PermissionScope.CODE_EXECUTION, risk=RiskLevel.CRITICAL)
    )
    assert result.decision is PermissionDecision.DENY


async def test_grant_allows_elevated_risk(runtime: Runtime) -> None:
    request = _request(risk=RiskLevel.HIGH)
    grant_id = await runtime.permissions.grant(request, granted_by="test")
    assert grant_id
    result = await runtime.permissions.check(request)
    assert result.decision is PermissionDecision.ALLOW
    assert result.grant_id == grant_id


# -- expiry: the predicate `check` and `list_effective_grants` share -----------


@pytest.mark.parametrize(
    "unparseable",
    ["not-a-timestamp", "", "   ", "2026-13-45T00:00:00+00:00", 12345],
)
def test_an_unparseable_expiry_counts_as_expired(unparseable: object) -> None:
    """A typo must not become a permanent grant.

    ``grant_is_expired`` says so in its docstring, and the direction matters more
    than the parse: a row whose ``expires_at`` cannot be read is a row whose
    lifetime nobody can state, so the only safe reading is "over". Reverting this
    branch to ``return False`` leaves the rest of the suite green, which is why it
    is asserted directly.
    """
    assert grant_is_expired(unparseable) is True  # type: ignore[arg-type]


def test_a_naive_expiry_is_read_as_utc_rather_than_raising() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert grant_is_expired("2000-01-01T00:00:00", now=now) is True
    assert grant_is_expired("2100-01-01T00:00:00", now=now) is False


def test_no_expiry_is_not_an_expiry() -> None:
    assert grant_is_expired(None) is False


# -- revocation must not depend on the shape of its argument -------------------


async def test_revocation_accepts_a_one_shot_iterator(runtime: Runtime) -> None:
    """``revoke`` reads its filter twice; a generator would be empty the second time.

    ``capabilities`` is an ``Iterable``, so ``(c for c in ...)`` is a legal
    argument. Consuming it once for the in-memory sets and again for the database
    is how a revoke deletes nothing while reporting success — and the caller has
    no way to tell, because the one-shot allowance it *did* clear is gone either
    way. The grant rows are the assertion that bites.
    """
    first = _request(adapter_id="iterator", capability="first", risk=RiskLevel.HIGH)
    second = _request(adapter_id="iterator", capability="second", risk=RiskLevel.HIGH)
    await runtime.permissions.grant(first, granted_by="test")
    await runtime.permissions.grant(second, granted_by="test")
    assert len(await runtime.permissions.list_grants()) == 2

    removed = await runtime.permissions.revoke("iterator", (name for name in ["first"]))

    assert len(removed) == 1
    remaining = [row["capability"] for row in await runtime.permissions.list_grants()]
    assert remaining == ["second"]
