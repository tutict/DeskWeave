"""Logging configuration.

In stdio MCP mode stdout is reserved for protocol frames, so logs are written to
**stderr** (or a file) by default and never to stdout.
"""

from __future__ import annotations

import logging
import sys
from typing import IO

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(
    level: str = "INFO",
    *,
    stream: IO[str] | None = None,
    force: bool = False,
) -> None:
    """Configure the root ``deskweave`` logger to write to ``stream``.

    ``stream`` defaults to ``sys.stderr`` to keep stdout clean for protocol use.
    """
    target = stream if stream is not None else sys.stderr
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))

    logger = logging.getLogger("deskweave")
    logger.setLevel(level.upper())
    if force:
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``deskweave`` namespace."""
    if name == "deskweave" or name.startswith("deskweave."):
        return logging.getLogger(name)
    return logging.getLogger(f"deskweave.{name}")


__all__ = ["DEFAULT_FORMAT", "configure_logging", "get_logger"]
