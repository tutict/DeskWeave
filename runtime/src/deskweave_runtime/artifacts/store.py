"""Artifact store.

Artifacts are addressed by logical id. When an artifact points at a local file
(a ``file://`` URI or a bare path), the store enforces that the path is inside
one of the configured *allowed roots*. The URI is never treated as an access
token: containment is re-checked on every read.

Remote images are imported through :class:`~.downloader.ImageDownloader`, which
owns the SSRF / size / content-type checks. The store only records the result.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from deskweave_contracts import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    Event,
    EventType,
    NotFoundError,
    PermissionDeniedError,
    UnsafeResourceError,
    new_artifact_id,
)

from ..events import EventBus
from ..persistence import ArtifactRepository
from .downloader import ImageDownloader

DEFAULT_PREVIEW_MAX_BYTES = 8 * 1024 * 1024


class ArtifactStore:
    def __init__(
        self,
        repository: ArtifactRepository,
        events: EventBus,
        *,
        allowed_roots: Sequence[Path] | None = None,
        downloader: ImageDownloader | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repo = repository
        self._events = events
        self._allowed_roots = tuple(Path(root).resolve() for root in (allowed_roots or ()))
        self._downloader = downloader
        self._logger = logger or logging.getLogger("deskweave.artifacts")

    # -- registration -----------------------------------------------------
    async def register(
        self,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        uri: str | None = None,
        media_type: str | None = None,
        size_bytes: int | None = None,
        metadata: ArtifactMetadata | None = None,
        task_id: str | None = None,
    ) -> ArtifactRef:
        """Register a new artifact, enforcing allowed-root constraints."""
        if uri is not None:
            self._check_uri(uri)
        artifact = ArtifactRef(
            artifact_id=new_artifact_id(),
            kind=kind,
            uri=uri,
            media_type=media_type,
            size_bytes=size_bytes,
            task_id=task_id,
            metadata=metadata or ArtifactMetadata(),
        )
        await self._repo.create(artifact)
        await self._emit_created(artifact)
        return artifact

    async def import_image(
        self,
        url: str,
        *,
        task_id: str,
        title: str | None = None,
        source: str | None = None,
        source_page: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> ArtifactRef:
        """Download ``url`` safely and register it as an image artifact.

        The artifact is only created once the bytes are on disk and validated, so
        a failed download never leaves a "successful" artifact behind.
        """
        if self._downloader is None:
            raise UnsafeResourceError(
                "artifact downloads are not configured for this runtime",
                details={"url": url},
            )

        artifact_id = new_artifact_id()
        downloaded = await self._downloader.download(url, artifact_id=artifact_id)
        metadata = ArtifactMetadata(
            title=title,
            source=source,
            source_page=source_page,
            original_url=url,
            sha256=downloaded.sha256,
            width=downloaded.width,
            height=downloaded.height,
            extra=extra or {},
        )
        artifact = ArtifactRef(
            artifact_id=artifact_id,
            kind=ArtifactKind.IMAGE,
            uri=downloaded.path.resolve().as_uri(),
            media_type=downloaded.media_type,
            size_bytes=downloaded.size_bytes,
            task_id=task_id,
            metadata=metadata,
        )
        await self._repo.create(artifact)
        await self._emit_created(artifact)
        self._logger.info(
            "imported artifact %s (%s, %d bytes)",
            artifact_id,
            downloaded.media_type,
            downloaded.size_bytes,
        )
        return artifact

    # -- reads ------------------------------------------------------------
    async def get(self, artifact_id: str) -> ArtifactRef:
        return await self._repo.get(artifact_id)

    async def list(self, *, limit: int | None = None) -> list[ArtifactRef]:
        return await self._repo.list(limit=limit)

    async def list_for_task(self, task_id: str) -> list[ArtifactRef]:
        return await self._repo.list_for_task(task_id)

    async def resolve_path(self, artifact_id: str) -> Path:
        """Resolve an artifact id to a verified local path.

        This is the only supported way for an adapter to obtain a file path.
        Callers pass ids across process/agent boundaries; paths never travel
        inbound.
        """
        artifact = await self._repo.get(artifact_id)
        if artifact.uri is None:
            raise UnsafeResourceError(
                f"artifact {artifact_id!r} has no local file",
                details={"artifact_id": artifact_id},
            )
        path = self._local_path(artifact.uri)
        if path is None or not path.is_file():
            raise NotFoundError(
                f"artifact file for {artifact_id!r} is missing",
                details={"artifact_id": artifact_id},
            )
        self._check_uri(artifact.uri)
        return path

    async def read_bytes(
        self, artifact_id: str, *, max_bytes: int = DEFAULT_PREVIEW_MAX_BYTES
    ) -> tuple[bytes, str]:
        """Read artifact bytes for previewing, bounded and root-checked."""
        artifact = await self._repo.get(artifact_id)
        path = await self.resolve_path(artifact_id)
        if artifact.size_bytes is not None and artifact.size_bytes > max_bytes:
            raise UnsafeResourceError(
                f"artifact is too large to preview ({artifact.size_bytes} bytes)",
                details={"artifact_id": artifact_id, "max_bytes": max_bytes},
            )
        size = path.stat().st_size
        if size > max_bytes:
            raise UnsafeResourceError(
                f"artifact is too large to preview ({size} bytes)",
                details={"artifact_id": artifact_id, "max_bytes": max_bytes},
            )
        return path.read_bytes(), artifact.media_type or "application/octet-stream"

    # -- internals --------------------------------------------------------
    async def _emit_created(self, artifact: ArtifactRef) -> None:
        await self._events.publish(
            Event(
                type=EventType.ARTIFACT_CREATED,
                payload={
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind.value,
                    "task_id": artifact.task_id,
                },
            )
        )

    def _check_uri(self, uri: str) -> None:
        path = self._local_path(uri)
        if path is None:
            # Non-file schemes (http:, s3:, ...) are not local paths.
            return
        if not self._allowed_roots:
            raise PermissionDeniedError(
                "file artifacts are not allowed: no allowed roots configured",
                details={"uri": uri},
            )
        resolved = path.resolve()
        for root in self._allowed_roots:
            if resolved == root or root in resolved.parents:
                return
        raise PermissionDeniedError(
            "artifact path is outside the allowed roots",
            details={"uri": uri, "allowed_roots": [str(r) for r in self._allowed_roots]},
        )

    @staticmethod
    def _local_path(uri: str) -> Path | None:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            return Path(url2pathname(parsed.path))
        if parsed.scheme == "":
            return Path(uri)
        return None


def ensure_artifact_root(data_dir: Path) -> Path:
    """Return ``<data_dir>/artifacts``, creating it if necessary."""
    root = (Path(data_dir) / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = [
    "DEFAULT_PREVIEW_MAX_BYTES",
    "ArtifactStore",
    "ensure_artifact_root",
]
