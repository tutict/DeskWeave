"""Wikimedia Commons search over the public MediaWiki API.

Why this source exists: the Bing collector parses a rendered HTML page, which is
brittle and returns images with unclear licensing. Wikimedia Commons exposes a
documented, key-less JSON API that returns the direct file URL, the dimensions and
the licence for every hit. No captcha, no scraping, no login — the request is
exactly the call the API documents.

The module is deliberately transport-thin so it can be tested without a network:

* :func:`build_params` / :func:`build_url` are pure;
* :func:`parse_response` turns an API payload into candidates and counts what it
  dropped (and why);
* :class:`WikimediaClient` only adds the HTTP call and the two-step search
  strategy.

Nothing here downloads image bytes — that is the runtime's artifact store, which
already enforces the SSRF/size/type policy. This module produces *candidates*, the
same shape the browser connector produces.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import Any

from deskweave_contracts import AdapterConnectionError, AdapterExecutionError

#: The only endpoint this connector talks to. Not configurable from a tool call:
#: an arbitrary URL here would be an SSRF primitive.
API_URL = "https://commons.wikimedia.org/w/api.php"

#: Wikimedia's API policy requires a descriptive User-Agent; the generic urllib one
#: is blocked, and silently pretending to be a browser is worse than identifying.
USER_AGENT = "DeskWeave/0.0.1 (local reference collector; +https://github.com/tutict/DeskWeave)"

#: Hosts a result may point at. Anything else is dropped before it can reach the
#: downloader, so a compromised response cannot steer the runtime elsewhere.
ALLOWED_IMAGE_HOSTS = frozenset(
    {"upload.wikimedia.org", "thumb.wikimedia.org", "commons.wikimedia.org"}
)

#: Exactly the formats the artifact downloader's sniffer accepts.
ALLOWED_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

#: Hard cap on the API response. A search result is a few tens of kilobytes; a
#: response larger than this is not a search result.
MAX_RESPONSE_BYTES = 2_000_000

#: Ask the API for a bounded thumbnail rather than the original: originals are
#: often 50+ MB, which exceeds the artifact downloader's 8 MiB limit.
THUMBNAIL_WIDTH = 1600

#: Metadata strings come from wikitext and may contain HTML entities and links.
MAX_METADATA_CHARS = 200
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

#: ``(url) -> body``. Injectable so the tests never touch the network.
Transport = Callable[[str], bytes]


@dataclass(frozen=True)
class WikimediaCandidate:
    """One downloadable image, in the shape the runtime's importer expects."""

    image_url: str
    page_url: str | None
    title: str
    width: int | None
    height: int | None
    license: str | None
    artist: str | None
    mime: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "image_url": self.image_url,
            "page_url": self.page_url,
            "title": self.title,
            "thumbnail_url": self.image_url,
            "width": self.width,
            "height": self.height,
            "media_type": self.mime,
            "license": self.license,
            "artist": self.artist,
        }


@dataclass(frozen=True)
class WikimediaSearch:
    """Outcome of a search, including what was filtered out."""

    candidates: list[WikimediaCandidate]
    query: str
    #: ``filetype`` when the server-side filter worked, ``plain`` on the fallback.
    strategy: str
    #: Results dropped because they were not a raster image or an unsafe URL.
    skipped: int


def build_params(query: str, limit: int, *, filetype_only: bool) -> dict[str, str]:
    """Build the MediaWiki query parameters for one search."""
    search = f"{query.strip()} filetype:bitmap" if filetype_only else query.strip()
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": search,
        "gsrnamespace": "6",  # File: namespace — images only, not articles
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": str(THUMBNAIL_WIDTH),
    }


def build_url(params: dict[str, str], *, api_url: str = API_URL) -> str:
    return f"{api_url}?{urllib.parse.urlencode(params)}"


def _clean(value: Any) -> str | None:
    """Turn an ``extmetadata`` value into plain text (tags stripped, bounded)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    text = _WHITESPACE.sub(" ", unescape(_TAGS.sub(" ", value))).strip()
    if not text:
        return None
    return text[:MAX_METADATA_CHARS]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the page list for both ``formatversion`` shapes."""
    query = payload.get("query") or {}
    pages = query.get("pages") if isinstance(query, dict) else None
    if isinstance(pages, dict):
        return [page for page in pages.values() if isinstance(page, dict)]
    if isinstance(pages, list):
        return [page for page in pages if isinstance(page, dict)]
    return []


def _is_safe_image_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in ALLOWED_IMAGE_HOSTS


def parse_response(payload: dict[str, Any], *, limit: int) -> tuple[list[WikimediaCandidate], int]:
    """Convert an API payload into candidates.

    Returns ``(candidates, skipped)`` where ``skipped`` counts results that were
    dropped because they were not a raster image or did not point at a Wikimedia
    host. Dropping is counted rather than silent: the caller reports it.
    """
    candidates: list[WikimediaCandidate] = []
    skipped = 0
    seen: set[str] = set()

    for page in _pages(payload):
        info = page.get("imageinfo")
        if not isinstance(info, list) or not info or not isinstance(info[0], dict):
            skipped += 1
            continue
        data = info[0]
        mime = str(data.get("mime") or "")
        if mime not in ALLOWED_MIMES:
            skipped += 1
            continue

        # Prefer the bounded thumbnail; fall back to the original file URL.
        url = str(data.get("thumburl") or data.get("url") or "")
        if not url or not _is_safe_image_url(url):
            skipped += 1
            continue
        if url in seen:
            skipped += 1
            continue
        seen.add(url)

        metadata = data.get("extmetadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        candidates.append(
            WikimediaCandidate(
                image_url=url,
                page_url=data.get("descriptionurl") or None,
                title=_clean(page.get("title")) or "untitled",
                width=_int_or_none(data.get("width")),
                height=_int_or_none(data.get("height")),
                license=_clean(metadata.get("LicenseShortName")),
                artist=_clean(metadata.get("Artist")),
                mime=mime,
            )
        )
        if len(candidates) >= limit:
            break

    return candidates, skipped


def _default_transport(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise AdapterExecutionError(
            "wikimedia response exceeded the size limit",
            details={"limit_bytes": MAX_RESPONSE_BYTES},
        )
    return body


class WikimediaClient:
    """Searches Commons. Owns nothing but the HTTP call and the retry strategy."""

    def __init__(
        self,
        *,
        transport: Callable[[str, float], bytes] | None = None,
        api_url: str = API_URL,
        timeout: float = 15.0,
    ) -> None:
        self._transport = transport or _default_transport
        self._api_url = api_url
        self._timeout = timeout

    async def search(self, query: str, limit: int) -> WikimediaSearch:
        """Search Commons, relaxing the server-side filter if it finds nothing.

        ``filetype:bitmap`` narrows the search to raster files, which is what we
        want; for niche queries the narrowed search can legitimately return zero
        hits, so the un-narrowed search is tried once. The strategy is reported so
        a caller can tell the difference between "no results" and "the filter was
        too strict".
        """
        candidates, skipped, strategy = await self._search_once(query, limit, filetype_only=True)
        if not candidates:
            candidates, plain_skipped, strategy = await self._search_once(
                query, limit, filetype_only=False
            )
            skipped += plain_skipped
        return WikimediaSearch(
            candidates=candidates, query=query, strategy=strategy, skipped=skipped
        )

    async def _search_once(
        self, query: str, limit: int, *, filetype_only: bool
    ) -> tuple[list[WikimediaCandidate], int, str]:
        url = build_url(
            build_params(query, limit, filetype_only=filetype_only), api_url=self._api_url
        )
        payload = await self._fetch_json(url)
        candidates, skipped = parse_response(payload, limit=limit)
        return candidates, skipped, "filetype" if filetype_only else "plain"

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        try:
            body = await asyncio.to_thread(self._transport, url, self._timeout)
        except AdapterExecutionError:
            raise
        except urllib.error.HTTPError as exc:
            raise AdapterConnectionError(
                f"wikimedia API returned HTTP {exc.code}",
                details={"status": exc.code},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdapterConnectionError(
                f"could not reach the wikimedia API: {type(exc).__name__}: {exc}",
                details={"reason": type(exc).__name__},
            ) from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise AdapterExecutionError(
                "wikimedia returned a response that is not JSON",
                details={"content_type": "non-json"},
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterExecutionError("wikimedia returned an unexpected JSON shape")
        if "error" in payload:
            # MediaWiki reports a well-formed error object; surface its code rather
            # than pretending the search simply found nothing.
            error = payload.get("error") or {}
            raise AdapterExecutionError(
                f"wikimedia API error: {error.get('code') or 'unknown'}",
                details={
                    "api_code": str(error.get("code") or ""),
                    "info": str(error.get("info") or "")[:200],
                },
            )
        return payload


__all__ = [
    "ALLOWED_IMAGE_HOSTS",
    "ALLOWED_MIMES",
    "API_URL",
    "MAX_RESPONSE_BYTES",
    "THUMBNAIL_WIDTH",
    "USER_AGENT",
    "WikimediaCandidate",
    "WikimediaClient",
    "WikimediaSearch",
    "build_params",
    "build_url",
    "parse_response",
]
