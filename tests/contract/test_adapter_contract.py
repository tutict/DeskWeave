"""Contract tests for the Adapter SDK.

These tests pin the behaviour every connector must provide: lifecycle, argument
validation, permission enforcement, error reporting, timeout and cleanup.
"""

from __future__ import annotations

import logging

from deskweave_adapter_sdk import Adapter, MockAdapter, validate_arguments
from deskweave_contracts import (
    AdapterConnectionError,
    AdapterStatus,
    Capability,
    OperationRequest,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    RiskLevel,
)


async def test_lifecycle() -> None:
    adapter = MockAdapter()
    descriptor = await adapter.discover()
    assert descriptor.adapter_id == "mock"
    assert await adapter.get_status() is AdapterStatus.DISCOVERED

    await adapter.connect()
    assert await adapter.get_status() is AdapterStatus.CONNECTED
    assert adapter.connect_calls == 1

    await adapter.disconnect()
    assert await adapter.get_status() is AdapterStatus.DISCONNECTED
    await adapter.disconnect()  # idempotent
    assert adapter.disconnect_calls == 2


async def test_capabilities_are_declared() -> None:
    adapter = MockAdapter()
    names = {capability.name for capability in await adapter.get_capabilities()}
    assert {"echo", "slow"} <= names


async def test_execute_success() -> None:
    adapter = MockAdapter()
    result = await adapter.execute(
        OperationRequest(capability="echo", arguments={"message": "hello"})
    )
    assert result.ok is True
    assert result.data == {"echo": "hello"}
    assert result.error is None


async def test_unknown_capability_is_reported() -> None:
    adapter = MockAdapter()
    result = await adapter.execute(OperationRequest(capability="nope"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"


async def test_invalid_arguments_are_reported() -> None:
    adapter = MockAdapter()
    result = await adapter.execute(OperationRequest(capability="echo", arguments={}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"


async def test_connect_failure_raises() -> None:
    adapter = MockAdapter(connect_error="boom")
    try:
        await adapter.connect()
    except AdapterConnectionError as exc:
        assert "boom" in exc.message
    else:  # pragma: no cover - defensive
        raise AssertionError("expected AdapterConnectionError")
    assert await adapter.get_status() is AdapterStatus.ERROR


async def test_run_failure_is_reported() -> None:
    adapter = MockAdapter(run_error="kaboom")
    result = await adapter.execute(OperationRequest(capability="echo", arguments={"message": "x"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_execution_error"


async def test_unexpected_exception_is_wrapped_not_swallowed() -> None:
    class Exploding(MockAdapter):
        async def _run(self, request: OperationRequest) -> dict:
            raise ValueError("unexpected")

    adapter = Exploding()
    result = await adapter.execute(OperationRequest(capability="echo", arguments={"message": "x"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_execution_error"
    assert result.error.details.get("type") == "ValueError"


async def test_timeout_is_reported() -> None:
    adapter = MockAdapter()
    result = await adapter.execute(
        OperationRequest(capability="slow", arguments={"milliseconds": 200}, timeout_seconds=0.02)
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_timeout_error"


async def test_elevated_capability_denied_without_checker() -> None:
    capability = Capability(name="danger", risk=RiskLevel.HIGH, parameters_schema={})
    adapter = MockAdapter(capabilities=[capability])
    result = await adapter.execute(OperationRequest(capability="danger"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"


async def test_permission_checker_deny() -> None:
    class Deny:
        async def check(self, request: PermissionRequest) -> PermissionResult:
            return PermissionResult(decision=PermissionDecision.DENY, reason="nope")

    capability = Capability(name="danger", risk=RiskLevel.HIGH, parameters_schema={})
    adapter = MockAdapter(capabilities=[capability], permission_checker=Deny())
    result = await adapter.execute(OperationRequest(capability="danger"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"


async def test_permission_checker_allow() -> None:
    class Allow:
        async def check(self, request: PermissionRequest) -> PermissionResult:
            return PermissionResult(decision=PermissionDecision.ALLOW)

    capability = Capability(name="danger", risk=RiskLevel.HIGH, parameters_schema={})
    adapter = MockAdapter(capabilities=[capability], permission_checker=Allow())
    result = await adapter.execute(OperationRequest(capability="danger"))
    assert result.ok is True


def test_validate_arguments_helper() -> None:
    capability = Capability(
        name="echo",
        parameters_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )
    validate_arguments(capability, {"message": "ok"})

    from deskweave_contracts import ValidationError

    try:
        validate_arguments(capability, {"message": 1})
    except ValidationError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ValidationError")


def test_adapter_is_abstract() -> None:
    import pytest

    with pytest.raises(TypeError):
        Adapter(descriptor=None)  # type: ignore[arg-type]


def test_logger_name_defaults() -> None:
    adapter = MockAdapter(adapter_id="custom")
    assert adapter._logger.name == "deskweave.adapter.custom"
    assert isinstance(adapter._logger, logging.Logger)
