"""Unit tests for the permission layer."""

from __future__ import annotations

from deskweave_contracts import (
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
    RiskLevel,
)
from deskweave_runtime import Runtime


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
