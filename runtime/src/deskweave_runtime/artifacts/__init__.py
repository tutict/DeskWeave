"""Artifact storage: registration, safe download, path resolution and opening."""

from __future__ import annotations

from .downloader import (
    DEFAULT_MAX_BYTES,
    DownloadedImage,
    ImageDownloader,
    sniff_image,
    validate_remote_url,
)
from .opener import ACTIONS, OPEN, REVEAL, ArtifactOpener, LaunchPlan, plan_launch
from .store import DEFAULT_PREVIEW_MAX_BYTES, ArtifactStore, ensure_artifact_root

__all__ = [
    "ACTIONS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_PREVIEW_MAX_BYTES",
    "OPEN",
    "REVEAL",
    "ArtifactOpener",
    "ArtifactStore",
    "DownloadedImage",
    "ImageDownloader",
    "LaunchPlan",
    "ensure_artifact_root",
    "plan_launch",
    "sniff_image",
    "validate_remote_url",
]
