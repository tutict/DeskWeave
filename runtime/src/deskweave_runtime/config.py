"""Runtime configuration.

Configuration is explicit and validated up front. The control API must bind to a
loopback address only; configuring a non-loopback host is rejected so that a
local desktop agent runtime is never accidentally exposed to the network.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deskweave_contracts import ConfigError

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class RuntimeConfig(BaseModel):
    """Validated runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default=Path(".deskweave"))
    database_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=0, le=65535)
    log_level: str = "INFO"
    token: str | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in LOOPBACK_HOSTS:
            raise ValueError(
                f"host {value!r} is not a loopback address; allowed: {sorted(LOOPBACK_HOSTS)}"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(VALID_LOG_LEVELS)}")
        return upper

    @property
    def resolved_database_path(self) -> Path:
        """Return the SQLite path, defaulting to ``<data_dir>/deskweave.db``."""
        if self.database_path is not None:
            return self.database_path
        return self.data_dir / "deskweave.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Build a config from environment variables.

        Raises :class:`~deskweave_contracts.ConfigError` on invalid values so the
        caller gets a diagnostic message instead of a stack trace.
        """
        source = os.environ if env is None else env
        raw: dict[str, object] = {}
        if (value := source.get("DESKWEAVE_DATA_DIR")) is not None:
            raw["data_dir"] = Path(value)
        if (value := source.get("DESKWEAVE_HOST")) is not None:
            raw["host"] = value
        if (value := source.get("DESKWEAVE_PORT")) is not None:
            try:
                raw["port"] = int(value)
            except ValueError as exc:
                raise ConfigError(f"DESKWEAVE_PORT must be an integer, got {value!r}") from exc
        if (value := source.get("DESKWEAVE_LOG_LEVEL")) is not None:
            raw["log_level"] = value
        if (value := source.get("DESKWEAVE_TOKEN")) is not None:
            raw["token"] = value or None

        try:
            return cls(**raw)
        except ValueError as exc:
            raise ConfigError(f"invalid runtime configuration: {exc}") from exc


__all__ = ["LOOPBACK_HOSTS", "VALID_LOG_LEVELS", "RuntimeConfig"]
