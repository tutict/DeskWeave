"""DeskWeave MCP gateway."""

from __future__ import annotations

from .server import INSTRUCTIONS, build_server, run_on_streams

__version__ = "0.0.1"

__all__ = ["INSTRUCTIONS", "__version__", "build_server", "run_on_streams"]
