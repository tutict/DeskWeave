"""DeskWeave filesystem connector."""

from __future__ import annotations

from .adapter import FILESYSTEM_CAPABILITIES, MAX_READ_BYTES, FilesystemAdapter

__all__ = ["FILESYSTEM_CAPABILITIES", "MAX_READ_BYTES", "FilesystemAdapter"]
