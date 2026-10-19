"""Anime modeling reference-workflow plugin.

Phase 0 registers the browser and Blender adapters with the runtime. The actual
workflow (search -> user confirmation -> reference plane) is implemented in a
later phase; this module only wires adapters and declares the manifest.
"""

from __future__ import annotations

from deskweave_adapter_blender import BlenderAdapter
from deskweave_adapter_browser import BrowserAdapter
from deskweave_plugin_sdk import Plugin, PluginContext, PluginManifest


class AnimeModelingPlugin(Plugin):
    """Registers the connectors used by the anime reference workflow."""

    def __init__(self, *, register_adapters: bool = True) -> None:
        self._register_adapters = register_adapters

    @property
    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="anime-modeling",
            name="Anime Modeling",
            version="0.0.1",
            description=(
                "Collect anime-character reference images and hand the confirmed "
                "selection to Blender as reference planes."
            ),
            required_capabilities=[
                "browser:collect_reference_images",
                "blender:create_reference_plane",
            ],
        )

    async def setup(self, context: PluginContext) -> None:
        if not self._register_adapters:
            return
        await context.register_adapter(BrowserAdapter())
        await context.register_adapter(BlenderAdapter())


__all__ = ["AnimeModelingPlugin"]
