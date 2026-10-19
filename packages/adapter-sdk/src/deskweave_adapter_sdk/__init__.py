"""DeskWeave Adapter SDK."""

from __future__ import annotations

from .base import Adapter, PermissionChecker, validate_arguments
from .mock import MockAdapter, default_capabilities
from .registry import AdapterRegistry

__version__ = "0.0.1"

__all__ = [
    "Adapter",
    "AdapterRegistry",
    "MockAdapter",
    "PermissionChecker",
    "__version__",
    "default_capabilities",
    "validate_arguments",
]
