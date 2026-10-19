"""Unit tests for the artifact store and allowed-root enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskweave_contracts import ArtifactKind, PermissionDeniedError
from deskweave_runtime import Runtime


async def test_register_non_file_uri(runtime: Runtime) -> None:
    artifact = await runtime.artifacts.register(
        kind=ArtifactKind.IMAGE, uri="https://example.com/ref.png", media_type="image/png"
    )
    assert artifact.artifact_id.startswith("art_")
    stored = await runtime.artifacts.get(artifact.artifact_id)
    assert stored.uri == "https://example.com/ref.png"


async def test_register_file_inside_allowed_root(runtime: Runtime, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "ref.png"
    target.write_bytes(b"png")
    artifact = await runtime.artifacts.register(kind=ArtifactKind.IMAGE, uri=target.as_uri())
    assert artifact.uri == target.as_uri()


async def test_register_file_outside_allowed_root_denied(runtime: Runtime, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    with pytest.raises(PermissionDeniedError):
        await runtime.artifacts.register(kind=ArtifactKind.IMAGE, uri=outside.as_uri())
