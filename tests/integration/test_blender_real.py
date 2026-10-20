"""Real Blender integration tests.

Opt-in because they launch Blender, which is a ~2 s process each (and twice per
assertion: create, then re-open to verify):

    DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py

Everything asserted here was read back from the ``.blend`` file that Blender
actually wrote — the creation script's own report is not trusted on its own.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from deskweave_adapter_blender import BlenderAdapter, find_blender
from deskweave_contracts import AdapterConnectionError, ArtifactKind, OperationRequest
from deskweave_runtime import Runtime

pytestmark = [
    pytest.mark.blender,
    pytest.mark.skipif(
        os.environ.get("DESKWEAVE_BLENDER_TESTS") != "1",
        reason="set DESKWEAVE_BLENDER_TESTS=1 to run tests that launch Blender",
    ),
]

CREATE = "create_reference_plane"


def png(width: int, height: int, seed: int) -> bytes:
    raw = b"".join(b"\x00" + bytes([seed % 256, 120, 200]) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
async def blender_env(runtime: Runtime):
    root = runtime.config.data_dir / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for index, (width, height) in enumerate([(320, 240), (200, 300)]):
        path = root / f"source-{index}.png"
        path.write_bytes(png(width, height, 40 * index))
        artifact = await runtime.artifacts.register(
            kind=ArtifactKind.IMAGE,
            uri=path.as_uri(),
            media_type="image/png",
            size_bytes=path.stat().st_size,
            task_id="task_blender",
        )
        ids.append(artifact.artifact_id)

    adapter = BlenderAdapter(output_dir=root)
    await runtime.register_adapter(adapter)
    await adapter.connect()
    try:
        yield runtime, adapter, ids
    finally:
        await adapter.disconnect()


async def _execute(runtime: Runtime, adapter: BlenderAdapter, ids: list[str], **extra):
    runtime.permissions.allow_once("blender", CREATE)
    return await adapter.execute(
        OperationRequest(
            capability=CREATE,
            arguments={"artifact_ids": ids, **extra},
            timeout_seconds=180,
            task_id="task_blender",
        )
    )


async def test_connect_reports_a_real_version(blender_env) -> None:
    _, adapter, _ = blender_env
    assert adapter.descriptor.capabilities[0].requires_confirmation is True
    assert (await adapter.get_status()).value == "connected"
    # Connecting twice is harmless.
    await adapter.connect()
    assert (await adapter.get_status()).value == "connected"


async def test_creates_textured_planes_and_saves_a_blend(blender_env) -> None:
    runtime, adapter, ids = blender_env

    result = await _execute(runtime, adapter, ids, name="ProbePlane")

    assert result.ok is True, result.error
    data = result.data
    blend = Path(data["blend_file"])
    assert blend.is_file()
    assert blend.suffix == ".blend"
    assert blend.stat().st_size > 1024

    plane = data["plane"]
    assert plane["image_count"] == 2
    assert len(plane["objects"]) == 2
    assert plane["dimensions_px"] == [[320, 240], [200, 300]]
    assert data["verification"]["opened"] is True
    assert data["verification"]["verified_objects"] == 2
    assert data["verification"]["object_count"] == 2
    assert data["blender_version"].split(".")[0].isdigit()


async def test_repeating_the_same_task_converges_instead_of_duplicating(blender_env) -> None:
    runtime, adapter, ids = blender_env

    first = await _execute(runtime, adapter, ids)
    second = await _execute(runtime, adapter, ids)

    assert first.ok is True and second.ok is True
    assert first.data["plane"]["objects"] == second.data["plane"]["objects"]
    assert second.data["idempotent_reuse"] is True
    # Verification re-reads the file: still two objects, not four.
    assert second.data["verification"]["object_count"] == 2


async def test_unknown_artifact_id_is_refused_before_blender_starts(blender_env) -> None:
    runtime, adapter, _ = blender_env
    root = runtime.config.data_dir / "artifacts"
    before = sorted(item.name for item in root.iterdir())

    runtime.permissions.allow_once("blender", CREATE)
    result = await adapter.execute(
        OperationRequest(
            capability=CREATE,
            arguments={"artifact_ids": ["art_does_not_exist"]},
            timeout_seconds=60,
            task_id="task_blender",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "not_found"
    assert sorted(item.name for item in root.iterdir()) == before


async def test_denied_without_a_one_shot_allowance(blender_env) -> None:
    runtime, adapter, ids = blender_env

    result = await adapter.execute(
        OperationRequest(
            capability=CREATE,
            arguments={"artifact_ids": ids},
            timeout_seconds=60,
            task_id="task_blender",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert not list((runtime.config.data_dir / "artifacts").glob("*.blend"))


async def test_invalid_arguments_never_reach_blender(blender_env) -> None:
    runtime, adapter, ids = blender_env

    for arguments in (
        {"artifact_ids": []},
        {"artifact_ids": ids, "height": 0},
        {"artifact_ids": ids, "name": "x" * 80},
        {"artifact_ids": ids, "python": "import os"},
        {"image_path": r"C:\Windows\win.ini"},
    ):
        runtime.permissions.allow_once("blender", CREATE)
        result = await adapter.execute(
            OperationRequest(capability=CREATE, arguments=arguments, task_id="task_blender")
        )
        assert result.ok is False, arguments
        assert result.error is not None
        assert result.error.code in {"validation_error", "permission_denied"}, arguments


async def test_missing_executable_is_reported(blender_env, tmp_path: Path) -> None:
    if find_blender() is None:  # pragma: no cover
        pytest.skip("no Blender installed")
    broken = BlenderAdapter(output_dir=tmp_path, executable=tmp_path / "no-blender-here")
    with pytest.raises(AdapterConnectionError) as excinfo:
        await broken.connect()
    assert "does not exist" in excinfo.value.message
