"""Stdio entry point for the DeskWeave MCP gateway.

stdout is reserved for MCP protocol frames; all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import sys

from deskweave_runtime import Runtime, RuntimeConfig, configure_logging, get_logger
from deskweave_runtime.bootstrap import register_default_adapters

from .server import build_server


async def _run() -> int:
    config = RuntimeConfig.from_env()
    configure_logging(config.log_level, force=True)
    logger = get_logger("mcp")

    runtime = await Runtime.create(config)
    await runtime.start()
    await register_default_adapters(runtime, logger=logger)
    try:
        server = build_server(runtime)
        logger.info("MCP gateway ready (db=%s)", config.resolved_database_path)
        await server.run_stdio_async()
    finally:
        await runtime.close()
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
