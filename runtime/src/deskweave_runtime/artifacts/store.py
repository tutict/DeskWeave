"""Artifact store.

Artifacts are addressed by logical id. When an artifact points at a local file
(a ``file://`` URI or a bare path), the store enforces that the path is inside
one of the configured *allowed roots*. The URI is never treated as an access
token: containment is re-checked on every registration.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from deskweave_contracts import (
    ArtifactKind,
    ArtifactRef,
    Event,
    EventType,
    PermissionDeniedError,
    new_artifact_id,
)

from ..events import EventBus
from ..persistence import ArtifactRepository


class ArtifactStore:
    def __init__(
        self,
        repository: ArtifactRepository,
        events: EventBus,
        *,
        allowed_roots: Sequence[Path] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repo = repository
        self._events = events
        self._allowed_roots = tuple(Path(root).resolve() for root in (allowed_roots or ()))
        self._logger = logger or logging.getLogger("deskweave.artifacts")

    async def register(
        self,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        uri: str | None = None,
        media_type: str | None = None,
        size_bytes: int | None = None,
        metadata: dict | None = None,
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
        )
        await self._repo.create(artifact, metadata)
        await self._events.publish(
            Event(
                type=EventType.ARTIFACT_CREATED,
                payload={"artifact_id": artifact.artifact_id, "kind": artifact.kind.value},
            )
        )
        return artifact

    async def get(self, artifact_id: str) -> ArtifactRef:
        return await self._repo.get(artifact_id)

    async def list(self, *, limit: int | None = None) -> list[ArtifactRef]:
        return await self._repo.list(limit=limit)

    def _check_uri(self, uri: str) -> None:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            path = Path(url2pathname(parsed.path))
        elif parsed.scheme == "":
            path = Path(uri)
        else:
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


__all__ = ["ArtifactStore"]
