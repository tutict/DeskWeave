"""Shared pytest fixtures.

Note: pytest's built-in ``tmp_path`` creates directories with ``mode=0o700``,
which is not usable in every environment. We override ``tmp_path`` with an
equivalent fixture that creates a plain directory inside the operating system's
temporary directory, keeping tests reproducible and self-contained.

The location matters. Test suites create and delete thousands of small files per
run, and a disposable directory under ``%TEMP%`` / ``/tmp`` is the one place
where "delete this whole tree" is an expected, cheap operation. Keeping that
churn out of the repository also means a crashed or killed run cannot leave
stale fixtures behind in the working tree. ``DESKWEAVE_TEST_TMP`` overrides the
base directory when a machine needs a different (still disposable) location.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from deskweave_adapter_sdk import MockAdapter
from deskweave_contracts import AdapterDescriptor, Capability, RiskLevel
from deskweave_runtime import Runtime, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Root for per-run test directories. Never inside the repository.
TMP_ROOT = Path(
    os.environ.get("DESKWEAVE_TEST_TMP") or (Path(tempfile.gettempdir()) / "deskweave-tests")
)


def _safe_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return cleaned[:60] or "test"


@pytest.fixture(scope="session")
def _session_tmp_root() -> Iterator[Path]:
    """One directory per pytest process, removed when the session ends.

    A per-session parent means an interrupted run leaves at most one directory
    behind (under the OS temp dir) instead of one per test inside the repo.
    """
    directory = TMP_ROOT / f"run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, _session_tmp_root: Path) -> Iterator[Path]:
    """A per-test directory that overrides pytest's builtin ``tmp_path``."""
    directory = _session_tmp_root / f"{_safe_name(request.node.name)}-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "deskweave-data"


@pytest.fixture
def config(data_dir: Path) -> RuntimeConfig:
    return RuntimeConfig(data_dir=data_dir, port=0)


@pytest.fixture
async def runtime(config: RuntimeConfig) -> AsyncIterator[Runtime]:
    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()


@pytest.fixture
def echo_capability() -> Capability:
    return Capability(
        name="echo",
        description="echo",
        parameters_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
    )


@pytest.fixture
def mock_adapter() -> MockAdapter:
    return MockAdapter()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def subprocess_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Environment for spawning DeskWeave subprocesses in tests."""
    import os

    env = dict(os.environ)
    env["DESKWEAVE_DATA_DIR"] = str(tmp_path / "subprocess-data")
    env["DESKWEAVE_PORT"] = "0"
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    yield env


@pytest.fixture
def python_executable() -> str:
    return sys.executable


__all__ = ["REPO_ROOT", "AdapterDescriptor"]
