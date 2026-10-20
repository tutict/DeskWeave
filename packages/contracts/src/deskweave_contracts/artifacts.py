"""Artifact contracts.

Artifacts are local materials and products (reference images, screenshots, logs,
Blender outputs). The model deliberately separates the *logical resource id*
(``artifact_id``, stable and shareable) from the *local file path*
(environment-specific and never an authorisation token).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArtifactKind(StrEnum):
    """Broad category of an artifact."""

    IMAGE = "image"
    SCREENSHOT = "screenshot"
    LOG = "log"
    MODEL = "model"
    DOCUMENT = "document"
    OTHER = "other"


class ArtifactMetadata(BaseModel):
    """Additional, kind-specific metadata for an artifact.

    ``original_url`` and ``source_page`` are retained for provenance: they explain
    where a downloaded file came from, but they are *not* access tokens and are
    never re-fetched automatically.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    source: str | None = Field(
        default=None, description="Source identifier, e.g. the search provider name."
    )
    source_page: str | None = Field(default=None, description="Page the candidate came from.")
    original_url: str | None = Field(
        default=None, description="Remote URL the file was fetched from."
    )
    sha256: str | None = Field(default=None, description="Digest of the stored bytes.")
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    """A reference to an artifact by logical id.

    ``uri`` is an opaque, environment-specific locator (e.g. a ``file://`` URI).
    It is descriptive metadata; access is always re-checked by the permission
    layer and is not granted merely by possessing the URI.

    ``task_id`` links an artifact to the workflow task that produced it. The link
    is what makes "confirm exactly these images for this task" checkable on the
    server, instead of trusting whatever ids the caller sends.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.OTHER
    uri: str | None = Field(
        default=None,
        description="Opaque locator such as file:///... ; not an access token.",
    )
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    task_id: str | None = Field(
        default=None, description="Task that produced this artifact, if any."
    )
    metadata: ArtifactMetadata = Field(default_factory=ArtifactMetadata)
    created_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactRef",
]
