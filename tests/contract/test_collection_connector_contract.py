"""The contract every *reference collection connector* must satisfy.

`tests/contract/test_adapter_contract.py` pins the SDK's behaviour through the mock
adapter. This file is the other half: the invariants a **real** connector has to
meet to be interchangeable with the others in the same role. A new source is
"done" only when it passes these — that is what makes adding a connector a
mechanical job instead of an act of faith.

The checks deliberately stop at the declaration/validation boundary: connecting to
Playwright or the network would make the suite depend on the machine, and each
connector already has its own tests for actually doing the work.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from deskweave_adapter_browser import BrowserAdapter
from deskweave_adapter_sdk import Adapter
from deskweave_adapter_wikimedia import WikimediaAdapter
from deskweave_contracts import (
    OperationRequest,
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
)
from deskweave_runtime.permissions import PermissionManager

COLLECTION_CAPABILITY = "collect_reference_images"

CONNECTORS: dict[str, Callable[[], Adapter]] = {
    "browser": BrowserAdapter,
    "wikimedia": WikimediaAdapter,
}


@pytest.fixture(params=sorted(CONNECTORS), ids=sorted(CONNECTORS))
def connector(request: pytest.FixtureRequest) -> Adapter:
    return CONNECTORS[request.param]()


async def test_the_role_uses_one_shared_capability_name(connector: Adapter) -> None:
    names = [capability.name for capability in await connector.get_capabilities()]
    assert names.count(COLLECTION_CAPABILITY) == 1, names


async def test_the_argument_contract_is_the_same_across_connectors(connector: Adapter) -> None:
    capability = next(
        item for item in await connector.get_capabilities() if item.name == COLLECTION_CAPABILITY
    )
    schema = capability.parameters_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) >= {"query", "limit", "source"}
    # Unknown fields must be impossible at the schema level, not filtered later.
    assert schema["additionalProperties"] is False


async def test_a_collection_connector_is_a_read_not_a_write(connector: Adapter) -> None:
    capability = next(
        item for item in await connector.get_capabilities() if item.name == COLLECTION_CAPABILITY
    )
    assert capability.requires_confirmation is False
    assert capability.scope in {PermissionScope.NETWORK, PermissionScope.BROWSER_CONTROL}
    # The one scope that must never appear on a connector capability.
    assert capability.scope is not PermissionScope.CODE_EXECUTION

    # The declaration and the shared policy have to agree, or a collection starts
    # failing at runtime with a permission decision. This is checked on behaviour
    # rather than on ``risk``: the browser connector is MODERATE (it drives a whole
    # browser) while a single documented API GET is READ_ONLY, and both must be
    # allowed without a prompt.
    policy = PermissionManager()
    decision = await policy.check(
        PermissionRequest(
            adapter_id=connector.adapter_id,
            capability=capability.name,
            scope=capability.scope,
            risk=capability.risk,
            requires_confirmation=capability.requires_confirmation,
        )
    )
    assert decision.decision is PermissionDecision.ALLOW, decision.reason


async def test_unknown_arguments_are_refused_before_any_work(connector: Adapter) -> None:
    result = await connector.execute(
        OperationRequest(
            capability=COLLECTION_CAPABILITY,
            arguments={"query": "elf armour", "url": "https://evil.example/x.png"},
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"


async def test_a_limit_outside_the_declared_range_is_refused(connector: Adapter) -> None:
    for limit in (0, 999):
        result = await connector.execute(
            OperationRequest(
                capability=COLLECTION_CAPABILITY, arguments={"query": "elf", "limit": limit}
            )
        )
        assert result.ok is False, limit
        assert result.error is not None
        assert result.error.code == "validation_error"


async def test_a_missing_query_is_refused(connector: Adapter) -> None:
    result = await connector.execute(
        OperationRequest(capability=COLLECTION_CAPABILITY, arguments={})
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"


async def test_the_descriptor_is_safe_to_show_to_an_agent(connector: Adapter) -> None:
    descriptor = await connector.discover()
    assert descriptor.adapter_id
    assert descriptor.name
    assert descriptor.capabilities
    # A descriptor is public surface: no paths, no URLs, no credentials.
    rendered = descriptor.model_dump_json()
    assert "C:\\\\" not in rendered
    assert "token" not in rendered.lower()
    assert "secret" not in rendered.lower()
