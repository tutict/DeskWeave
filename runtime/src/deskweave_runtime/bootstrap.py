"""Composition root for the shipped connectors.

The core runtime deliberately knows nothing about browsers, Blender or Wikimedia:
it exposes an adapter registry and lets the application decide what to plug in.
This module is that decision, and it is the only place that imports concrete
adapters.

Two things are wired per connector:

* registration — the adapter becomes discoverable and its descriptor shows up in
  ``/adapters``;
* its *role*, if it fills one — e.g. which connector serves the ``wikimedia``
  reference source. Roles are declared here, never inside the runtime, so adding a
  connector is an edit to this file plus a new package.

Adapters are registered even when their external dependency is missing, so the UI
can show an honest ``error`` status instead of a missing capability. ``connect()``
failures are logged, not fatal: a machine without Blender can still collect
reference images.
"""

from __future__ import annotations

import logging

from deskweave_contracts import DeskWeaveError

from .artifacts import ensure_artifact_root
from .runtime import Runtime


async def register_default_adapters(runtime: Runtime, *, logger: logging.Logger) -> list[str]:
    """Register the shipped connectors. Returns their adapter ids."""
    registered: list[str] = []
    artifact_root = ensure_artifact_root(runtime.config.data_dir)

    adapters = []
    try:
        from deskweave_adapter_browser import BrowserAdapter

        adapters.append(BrowserAdapter())
    except ImportError as exc:  # pragma: no cover - packaging failure
        logger.warning("browser connector is not installed: %s", exc)

    try:
        from deskweave_adapter_blender import BlenderAdapter

        adapters.append(BlenderAdapter(output_dir=artifact_root))
    except ImportError as exc:  # pragma: no cover - packaging failure
        logger.warning("blender connector is not installed: %s", exc)

    try:
        from deskweave_adapter_wikimedia import WikimediaAdapter

        adapters.append(WikimediaAdapter())
    except ImportError as exc:  # pragma: no cover - packaging failure
        logger.warning("wikimedia connector is not installed: %s", exc)

    for adapter in adapters:
        await runtime.register_adapter(adapter, replace=True)
        try:
            await adapter.connect()
        except DeskWeaveError as exc:
            logger.warning("adapter %s is registered but unavailable: %s", adapter.adapter_id, exc)
        registered.append(adapter.adapter_id)

    # The ``bing`` source is served by the browser connector and needs no wiring.
    # Any other source has to exist as a registered adapter before it is announced.
    if "wikimedia" in registered:
        runtime.register_collection_source("wikimedia", "wikimedia")

    return registered


__all__ = ["register_default_adapters"]
