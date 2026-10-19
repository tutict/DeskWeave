"""Integration test: a real (read-only) filesystem adapter through the runtime."""

from __future__ import annotations

from pathlib import Path

from deskweave_adapter_filesystem import FilesystemAdapter
from deskweave_contracts import OperationRequest
from deskweave_runtime import Runtime


async def _adapter(runtime: Runtime, root: Path) -> FilesystemAdapter:
    adapter = FilesystemAdapter(allowed_roots=[root])
    await runtime.register_adapter(adapter)
    await adapter.connect()
    return adapter


async def test_list_dir(runtime: Runtime, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "a.txt").write_text("hello", encoding="utf-8")
    await _adapter(runtime, data_dir)

    result = await runtime.execute(
        "filesystem", OperationRequest(capability="list_dir", arguments={"path": str(data_dir)})
    )
    assert result.ok is True
    assert "a.txt" in result.data["entries"]


async def test_read_text(runtime: Runtime, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "note.txt"
    target.write_text("deskweave", encoding="utf-8")
    await _adapter(runtime, data_dir)

    result = await runtime.execute(
        "filesystem", OperationRequest(capability="read_text", arguments={"path": str(target)})
    )
    assert result.ok is True
    assert result.data["text"] == "deskweave"


async def test_read_outside_allowed_root_denied(
    runtime: Runtime, data_dir: Path, tmp_path: Path
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    await _adapter(runtime, data_dir)

    result = await runtime.execute(
        "filesystem", OperationRequest(capability="read_text", arguments={"path": str(outside)})
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"
