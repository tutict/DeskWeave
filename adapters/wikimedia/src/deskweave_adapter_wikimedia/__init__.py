"""DeskWeave Wikimedia Commons connector."""

from __future__ import annotations

from .adapter import COLLECTION_CAPABILITY, WIKIMEDIA_CAPABILITIES, WikimediaAdapter
from .api import WikimediaCandidate, WikimediaClient, WikimediaSearch

__all__ = [
    "COLLECTION_CAPABILITY",
    "WIKIMEDIA_CAPABILITIES",
    "WikimediaAdapter",
    "WikimediaCandidate",
    "WikimediaClient",
    "WikimediaSearch",
]
