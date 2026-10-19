"""Unit tests for the runtime lifecycle, health and execution path."""

from __future__ import annotations

import pytest

from deskweave_adapter_sdk import MockAdapter
from deskweave_contracts import (
    LifecycleError,
    NotFoundError,
    OperationRequest,
)
from deskweave_runtime import Runtime, RuntimeConfig, RuntimeState


async def test_create_start_close(config: RuntimeConfig) -> None:
    runtime = await Runtime.create(config)
    assert runtime.state is RuntimeState.CREATED
    await runtime.start()
    assert runtime.state is RuntimeState.RUNNING
    await runtime.close()
    assert runtime.state is RuntimeState.CLOSED


async def test_double_start_raises(config: RuntimeConfig) -> None:
    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        with pytest.raises(LifecycleError):
            await runtime.start()
    finally:
        await runtime.close()


async def test_start_after_close_raises(config: RuntimeConfig) -> None:
    runtime = await Runtime.create(config)
    await runtime.start()
    await runtime.close()
    with pytest.raises(LifecycleError):
        await runtime.start()


async def test_close_is_idempotent(config: RuntimeConfig) -> None:
    runtime = await Runtime.create(config)
    await runtime.start()
    await runtime.close()
    await runtime.close()


async def test_health_reports_registered_adapters(runtime: Runtime) -> None:
    await runtime.register_adapter(MockAdapter())
    health = await runtime.health()
    assert health.state.value == "ok"
    assert health.schema_version >= 1
    assert [adapter.adapter_id for adapter in health.adapters] == ["mock"]
    assert "echo" in health.adapters[0].capabilities


async def test_duplicate_adapter_registration_raises(runtime: Runtime) -> None:
    from deskweave_contracts import ConflictError

    await runtime.register_adapter(MockAdapter())
    with pytest.raises(ConflictError):
        await runtime.register_adapter(MockAdapter())


async def test_unregister_adapter(runtime: Runtime) -> None:
    await runtime.register_adapter(MockAdapter())
    await runtime.unregister_adapter("mock")
    assert await runtime.list_adapters() == []
    with pytest.raises(NotFoundError):
        await runtime.unregister_adapter("mock")


async def test_execute_records_execution(runtime: Runtime) -> None:
    await runtime.register_adapter(MockAdapter())
    result = await runtime.execute(
        "mock", OperationRequest(capability="echo", arguments={"message": "hi"})
    )
    assert result.ok is True
    records = await runtime.executions.list()
    assert len(records) == 1
    assert records[0].adapter_id == "mock"
    assert records[0].ok is True


async def test_execute_unknown_adapter_raises(runtime: Runtime) -> None:
    with pytest.raises(NotFoundError):
        await runtime.execute("nope", OperationRequest(capability="echo"))


async def test_close_disconnects_adapters(config: RuntimeConfig) -> None:
    runtime = await Runtime.create(config)
    await runtime.start()
    adapter = MockAdapter()
    await runtime.register_adapter(adapter)
    await adapter.connect()
    await runtime.close()
    assert adapter.disconnect_calls == 1
