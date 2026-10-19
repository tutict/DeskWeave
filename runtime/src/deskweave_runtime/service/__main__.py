"""Command line entry point for the DeskWeave local runtime service.

Examples
--------
Run on the default loopback port and announce a handshake on stdout::

    python -m deskweave_runtime.service --announce

The handshake line is a single JSON object::

    {"event": "ready", "host": "127.0.0.1", "port": 8787, "pid": 1234}

The shared token is written to ``<data_dir>/runtime.json``; it is never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import signal
import sys
from pathlib import Path

from ..config import RuntimeConfig
from ..logging import configure_logging, get_logger
from ..runtime import Runtime
from .app import RuntimeService


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="deskweave-runtime", description=__doc__)
    parser.add_argument("--host", default=None, help="loopback host to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port to bind (0 = ephemeral)")
    parser.add_argument("--data-dir", default=None, help="directory for the database and state")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR|CRITICAL")
    parser.add_argument("--token", default=None, help="shared token (generated if omitted)")
    parser.add_argument(
        "--announce",
        action="store_true",
        help="print a JSON handshake line on stdout once the server is ready",
    )
    return parser.parse_args(argv)


def _build_config(args: argparse.Namespace) -> RuntimeConfig:
    base = RuntimeConfig.from_env()
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.data_dir is not None:
        overrides["data_dir"] = Path(args.data_dir)
    if args.log_level is not None:
        overrides["log_level"] = args.log_level
    if args.token is not None:
        overrides["token"] = args.token or None
    return base.model_copy(update=overrides)


async def _run(args: argparse.Namespace) -> int:
    config = _build_config(args)
    configure_logging(config.log_level, force=True)
    logger = get_logger("service.main")

    token = config.token or secrets.token_urlsafe(32)

    runtime = await Runtime.create(config)
    await runtime.start()

    service = RuntimeService(runtime, token=token, host=config.host, port=config.port)
    await service.start()

    _write_state_file(config, service.port, service.url, token)

    if args.announce:
        print(
            json.dumps(
                {"event": "ready", "host": config.host, "port": service.port, "pid": _pid()}
            ),
            flush=True,
        )

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, service)

    logger.info("runtime service ready at %s", service.url)
    try:
        await service.wait_shutdown()
    finally:
        await service.stop()
        await runtime.close()
    return 0


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, service: RuntimeService) -> None:
    def _handler(signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(service.request_shutdown)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            continue


def _write_state_file(config: RuntimeConfig, port: int, url: str, token: str) -> None:
    state_path = config.data_dir / "runtime.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"host": config.host, "port": port, "url": url, "token": token, "pid": _pid()}),
        encoding="utf-8",
    )


def _pid() -> int:
    import os

    return os.getpid()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
