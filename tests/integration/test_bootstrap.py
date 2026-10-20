"""The composition root: which connectors ship, and which sources they serve.

`bootstrap.py` is the only module allowed to name concrete connectors, so these
tests are the place where "a missing connector degrades, it does not break"
becomes an assertion instead of a claim.
"""

from __future__ import annotations

import logging
import sys

import pytest

from deskweave_contracts import AdapterConnectionError, CollectionRequest
from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.bootstrap import register_default_adapters

LOGGER = logging.getLogger("test.bootstrap")


@pytest.fixture
async def booted(config: RuntimeConfig) -> Runtime:
    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        await register_default_adapters(runtime, logger=LOGGER)
        yield runtime
    finally:
        await runtime.close()


async def test_the_shipped_connectors_are_registered(booted: Runtime) -> None:
    ids = {descriptor.adapter_id for descriptor in await booted.list_adapters()}
    assert ids == {"browser", "blender", "wikimedia"}
    for descriptor in await booted.list_adapters():
        assert descriptor.capabilities, descriptor.adapter_id


async def test_each_reference_source_resolves_to_its_connector(booted: Runtime) -> None:
    # `bing` is the built-in default; `wikimedia` is wired by the composition root.
    assert booted.references.adapter_id_for_source("bing") == "browser"
    assert booted.references.adapter_id_for_source("wikimedia") == "wikimedia"

    # Both connectors really offer the capability the service is about to call.
    for adapter_id in ("browser", "wikimedia"):
        descriptor = booted.registry.get(adapter_id).descriptor
        assert "collect_reference_images" in {
            capability.name for capability in descriptor.capabilities
        }


async def test_an_uninstalled_connector_does_not_take_the_others_down(
    config: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing package is a missing capability, not a failed startup."""
    # ``None`` in sys.modules makes the import raise ImportError, exactly like an
    # uninstalled distribution — without uninstalling anything.
    monkeypatch.setitem(sys.modules, "deskweave_adapter_wikimedia", None)

    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        registered = await register_default_adapters(runtime, logger=LOGGER)
        assert "wikimedia" not in registered
        assert {"browser", "blender"} <= set(registered)

        # The source is now simply unserved, and says so before creating a task.
        with pytest.raises(AdapterConnectionError) as excinfo:
            await runtime.references.start_collection(
                CollectionRequest(query="elf armor", limit=1, source="wikimedia")
            )
        assert excinfo.value.details["source"] == "wikimedia"
    finally:
        await runtime.close()


async def test_a_valid_source_with_no_connector_keeps_the_default_working(
    config: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative case above must not be achieved by breaking the default path."""
    monkeypatch.setitem(sys.modules, "deskweave_adapter_wikimedia", None)

    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        await register_default_adapters(runtime, logger=LOGGER)
        assert runtime.references.adapter_id_for_source("bing") == "browser"
    finally:
        await runtime.close()
