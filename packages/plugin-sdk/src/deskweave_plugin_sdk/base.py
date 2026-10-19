"""Plugin contract.

A plugin bundles cross-application workflow logic. It interacts with the runtime
only through a :class:`PluginContext`, which exposes narrow registration hooks
and read-only views. Plugins must not access the core database directly or bypass
the permission layer.

The runtime satisfies this protocol structurally (it already exposes
``register_adapter`` and ``list_adapters``), so no runtime -> plugin-sdk
dependency is required.
"""

from __future__ import annotations

import abc
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from deskweave_contracts import AdapterDescriptor


class PluginManifest(BaseModel):
    """Static, declarative description of a plugin."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    name: str
    version: str = "0.0.0"
    description: str = ""
    required_capabilities: list[str] = Field(default_factory=list)


@runtime_checkable
class PluginContext(Protocol):
    """The restricted surface a plugin is given by the runtime."""

    async def register_adapter(self, adapter: object) -> None:
        """Register an adapter instance with the runtime registry."""
        ...

    async def list_adapters(self) -> list[AdapterDescriptor]:
        """Return read-only descriptors of currently registered adapters."""
        ...


class Plugin(abc.ABC):
    """Base class for DeskWeave plugins."""

    @property
    @abc.abstractmethod
    def manifest(self) -> PluginManifest:
        """Return the plugin manifest."""

    @abc.abstractmethod
    async def setup(self, context: PluginContext) -> None:
        """Wire the plugin into the runtime using ``context``."""

    async def teardown(self) -> None:
        """Release plugin resources. Default is a no-op."""
        return None


__all__ = ["Plugin", "PluginContext", "PluginManifest"]
