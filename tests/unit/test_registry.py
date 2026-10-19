"""Unit tests for the adapter registry."""

from __future__ import annotations

import pytest

from deskweave_adapter_sdk import AdapterRegistry, MockAdapter
from deskweave_contracts import ConflictError, NotFoundError


def test_register_and_lookup() -> None:
    registry = AdapterRegistry()
    adapter = MockAdapter()
    registry.register(adapter)
    assert "mock" in registry
    assert len(registry) == 1
    assert registry.get("mock") is adapter
    assert registry.list_ids() == ["mock"]
    assert registry.list_descriptors()[0].adapter_id == "mock"


def test_duplicate_registration_conflicts() -> None:
    registry = AdapterRegistry()
    registry.register(MockAdapter())
    with pytest.raises(ConflictError):
        registry.register(MockAdapter())


def test_replace_allows_duplicate() -> None:
    registry = AdapterRegistry()
    first = MockAdapter()
    second = MockAdapter()
    registry.register(first)
    registry.register(second, replace=True)
    assert registry.get("mock") is second


def test_unregister_unknown_raises() -> None:
    registry = AdapterRegistry()
    with pytest.raises(NotFoundError):
        registry.unregister("missing")


def test_get_unknown_raises() -> None:
    registry = AdapterRegistry()
    with pytest.raises(NotFoundError):
        registry.get("missing")
