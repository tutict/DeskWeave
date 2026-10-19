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


class ArtifactRef(BaseModel):
    """A reference to an artifact by logical id.

    ``uri`` is an opaque, environment-specific locator (e.g. a ``file://`` URI).
    It is descriptive metadata; access is always re-checked by the permission
    layer and is not granted merely by possessing the URI.
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
    created_at: datetime = Field(default_factory=_utcnow)


class ArtifactMetadata(BaseModel):
    """Additional, kind-specific metadata for an artifact."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    source: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactRef",
]
