"""DeskWeave local core runtime."""

from __future__ import annotations

from . import artifacts, core, events, permissions, persistence, workflow
from .config import RuntimeConfig
from .logging import configure_logging, get_logger
from .runtime import Runtime, RuntimeState, open_runtime

__version__ = "0.0.1"

__all__ = [
    "Runtime",
    "RuntimeConfig",
    "RuntimeState",
    "__version__",
    "artifacts",
    "configure_logging",
    "core",
    "events",
    "get_logger",
    "open_runtime",
    "permissions",
    "persistence",
    "workflow",
]
