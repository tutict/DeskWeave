"""Unit tests for runtime configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskweave_contracts import ConfigError
from deskweave_runtime import RuntimeConfig


def test_defaults(tmp_path: Path) -> None:
    config = RuntimeConfig(data_dir=tmp_path)
    assert config.host == "127.0.0.1"
    assert config.port == 8787
    assert config.log_level == "INFO"
    assert config.resolved_database_path == tmp_path / "deskweave.db"


def test_rejects_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(data_dir=tmp_path, host="0.0.0.0")


def test_rejects_invalid_port(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(data_dir=tmp_path, port=70000)


def test_rejects_invalid_log_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(data_dir=tmp_path, log_level="LOUD")


def test_from_env(tmp_path: Path) -> None:
    config = RuntimeConfig.from_env(
        {
            "DESKWEAVE_DATA_DIR": str(tmp_path),
            "DESKWEAVE_HOST": "localhost",
            "DESKWEAVE_PORT": "9000",
            "DESKWEAVE_LOG_LEVEL": "debug",
            "DESKWEAVE_TOKEN": "secret",
        }
    )
    assert config.data_dir == tmp_path
    assert config.host == "localhost"
    assert config.port == 9000
    assert config.log_level == "DEBUG"
    assert config.token == "secret"


def test_from_env_invalid_port_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env({"DESKWEAVE_PORT": "not-a-number"})


def test_from_env_invalid_host_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env({"DESKWEAVE_DATA_DIR": str(tmp_path), "DESKWEAVE_HOST": "10.0.0.1"})
