"""In-memory doubles shared by the workflow and control-API tests.

Only the *transport* is simulated (network, Blender). The doubles still go through
``Adapter.execute`` and therefore through the real permission layer, so tests that
use them exercise the production trust boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib
from pathlib import Path
from typing import Any

from deskweave_adapter_sdk import Adapter
from deskweave_contracts import (
    AdapterDescriptor,
    Capability,
    DeskWeaveError,
    PermissionScope,
    RiskLevel,
    UnsafeResourceError,
)
from deskweave_runtime.artifacts.downloader import DownloadedImage

COLLECT = "collect_reference_images"
CREATE_PLANE = "create_reference_plane"


class SimulatedCrash(BaseException):
    """Fakes the process dying mid-operation.

    Derives from ``BaseException`` so the workflow's ``except Exception`` handlers
    (which exist to *report* failures) do not swallow it: the durable state is then
    exactly what a hard kill leaves. Deliberately not ``SystemExit`` — asyncio
    re-raises that into the event loop and would abort the test run.
    """


def make_png(width: int = 6, height: int = 4, seed: int = 0) -> bytes:
    """Build a real, minimal RGB PNG."""
    raw = b"".join(b"\x00" + bytes([seed % 256, 90, 160]) * width for _ in range(height))

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


class FakeDownloader:
    """Stands in for :class:`ImageDownloader`; writes real PNG bytes to disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.requested: list[str] = []

    async def download(self, url: str, *, artifact_id: str) -> DownloadedImage:
        self.requested.append(url)
        if "broken" in url:
            raise UnsafeResourceError("simulated fetch failure", details={"url": url})
        body = make_png(seed=len(url))
        path = self.root / f"{artifact_id}.png"
        path.write_bytes(body)
        return DownloadedImage(
            path=path,
            media_type="image/png",
            extension=".png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            width=6,
            height=4,
        )


class FakeBrowser(Adapter):
    def __init__(
        self,
        candidates: list[dict[str, Any]] | None = None,
        *,
        delay: float = 0.0,
        adapter_id: str = "browser",
        default_source: str = "bing",
    ):
        self.candidates = candidates if candidates is not None else []
        self.delay = delay
        self.default_source = default_source
        self.calls = 0
        super().__init__(
            AdapterDescriptor(
                adapter_id=adapter_id,
                name="Fake Browser",
                capabilities=[
                    Capability(
                        name=COLLECT,
                        parameters_schema={
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                                "source": {"type": "string"},
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                        risk=RiskLevel.MODERATE,
                        scope=PermissionScope.BROWSER_CONTROL,
                    )
                ],
            )
        )

    async def connect(self) -> None:
        return None

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        return None

    async def _run(self, request: Any) -> dict[str, Any]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            "query": request.arguments["query"],
            "source": request.arguments.get("source", self.default_source),
            "candidates": self.candidates,
        }


class FakeBlender(Adapter):
    """A target connector that resolves artifacts and writes a stand-in .blend.

    It implements the same optional recovery contract as the real connector
    (``find_output`` / ``verify_output``), and the output path is derived from the
    task id exactly like the real adapter does — so a restart can find the file a
    crashed attempt left behind.

    ``crash_after_write`` simulates a process kill *between* "the target
    application wrote its output" and "the runtime recorded the result", which is
    the only interruption that leaves ambiguous state on disk.
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        fail: bool = False,
        crash_after_write: bool = False,
        verify_error: str | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.fail = fail
        self.crash_after_write = crash_after_write
        self.verify_error = verify_error
        self.calls: list[list[str]] = []
        self.resolved: list[Path] = []
        self.verified: list[Path] = []
        super().__init__(
            AdapterDescriptor(
                adapter_id="blender",
                name="Fake Blender",
                capabilities=[
                    Capability(
                        name=CREATE_PLANE,
                        parameters_schema={
                            "type": "object",
                            "properties": {
                                "artifact_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "maxItems": 20,
                                },
                                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                            },
                            "required": ["artifact_ids"],
                            "additionalProperties": False,
                        },
                        risk=RiskLevel.HIGH,
                        scope=PermissionScope.BLENDER_CONTROL,
                        requires_confirmation=True,
                    )
                ],
            )
        )

    async def connect(self) -> None:
        return None

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        return None

    def output_path(self, task_id: str | None) -> Path:
        return self.output_dir / f"plane-{task_id or 'manual'}.blend"

    async def _run(self, request: Any) -> dict[str, Any]:
        ids = list(request.arguments["artifact_ids"])
        self.calls.append(ids)
        if self.fail:
            raise DeskWeaveError("simulated blender failure")
        paths = [await self.resolve_artifact(artifact_id) for artifact_id in ids]
        self.resolved.extend(paths)
        output = self.output_path(request.task_id)
        output.write_bytes(b"BLENDER-fake")
        if self.crash_after_write:
            # The file exists; the runtime will never learn about it.
            raise SimulatedCrash("simulated hard kill after the scene was written")
        return {
            "plane": {
                "name": request.arguments.get("name") or "ReferencePlane",
                "image_count": len(paths),
                "dimensions_px": [[6, 4] for _ in paths],
            },
            "blend_file": str(output),
        }

    # -- recovery contract -------------------------------------------------
    async def find_output(self, *, task_id: str, arguments: dict[str, Any]) -> Path | None:
        output = self.output_path(task_id)
        return output if output.is_file() else None

    async def verify_output(self, path: Path, *, arguments: dict[str, Any]) -> dict[str, Any]:
        self.verified.append(path)
        if self.verify_error is not None:
            raise DeskWeaveError(self.verify_error)
        if path.read_bytes() != b"BLENDER-fake":
            raise DeskWeaveError("simulated corrupt blend file")
        return {
            "opened": True,
            "verified_objects": len(arguments.get("artifact_ids") or []),
            "recovered": True,
        }


def candidates(count: int = 2, *, broken: bool = False) -> list[dict[str, Any]]:
    items = [
        {
            "image_url": f"https://cdn.example.com/ref-{index}.png",
            "page_url": f"https://example.com/page-{index}",
            "title": f"Reference {index}",
            "thumbnail_url": f"https://cdn.example.com/thumb-{index}.png",
            "width": 600,
            "height": 400,
        }
        for index in range(count)
    ]
    if broken:
        items.append(
            {
                "image_url": "https://cdn.example.com/broken.png",
                "page_url": "https://example.com/page-broken",
                "title": "Broken",
            }
        )
    return items


async def install_fakes(runtime: Any) -> tuple[FakeBrowser, FakeBlender, FakeDownloader]:
    """Swap the download transport and register both fake adapters."""
    downloader = FakeDownloader(runtime.config.data_dir / "artifacts")
    runtime.artifacts._downloader = downloader  # type: ignore[attr-defined]
    browser = FakeBrowser()
    blender = FakeBlender(runtime.config.data_dir / "artifacts")
    await runtime.register_adapter(browser)
    await runtime.register_adapter(blender)
    return browser, blender, downloader


__all__ = [
    "COLLECT",
    "CREATE_PLANE",
    "FakeBlender",
    "FakeBrowser",
    "FakeDownloader",
    "SimulatedCrash",
    "candidates",
    "install_fakes",
    "make_png",
]
