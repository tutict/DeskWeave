"""Image search sources.

Parsing lives here, separate from the adapter and from the runtime: a source
turns a Playwright page into a list of :class:`ImageCandidate` values. Adding a
provider means adding a class, not touching the adapter's lifecycle code.

Only ``bing`` is implemented. Google and DuckDuckGo are unreachable in the
development environment this was built in, and shipping an untested parser would
be worse than shipping one working source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from deskweave_contracts import AdapterExecutionError, BrowserAccessDeniedError

#: Elements Bing uses for image results; each carries a JSON blob in ``m``.
_RESULT_SELECTOR = "a.iusc"

#: Markers in the page *title*. Titles are short and provider-controlled, so
#: matching here is safe and catches interstitial challenge pages.
_TITLE_MARKERS = (
    "captcha",
    "验证码",
    "人机验证",
    "attention required",
    "just a moment",
    "access denied",
    "cloudflare",
)
#: Markers in the page *body*. Deliberately narrower than the title list: a
#: legitimate result page may mention captchas in prose.
_BODY_MARKERS = (
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "请输入验证码",
    "请完成安全验证",
    "异常流量",
)


@dataclass(frozen=True)
class ImageCandidate:
    """One search result, before it has been downloaded."""

    image_url: str
    page_url: str | None = None
    title: str | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None


class ImageSource(Protocol):
    """A provider-specific parser driven by a Playwright page."""

    name: str

    async def search(self, page: Any, query: str, limit: int) -> list[ImageCandidate]: ...


class BingImageSource:
    """Parses Bing Images result cards (``a.iusc[m]`` JSON blobs)."""

    name = "bing"
    search_url = "https://cn.bing.com/images/search"

    def build_url(self, query: str) -> str:
        """Return the results URL for ``query``.

        The query **must** be part of the URL: without it Bing serves its
        front-page "trending" images, which look like real results but have
        nothing to do with the request.
        """
        return f"{self.search_url}?{urlencode({'q': query, 'form': 'HDRSC2', 'first': 1})}"

    async def search(self, page: Any, query: str, limit: int) -> list[ImageCandidate]:
        try:
            response = await page.goto(
                self.build_url(query),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as exc:
            # Navigation failures (DNS, TLS, blocked network, timeout) must be
            # reported with the provider and the reason, never as empty results.
            raise AdapterExecutionError(
                f"could not open the image search page: {type(exc).__name__}: {exc}",
                details={"source": self.name, "query": query},
            ) from exc
        if response is not None and response.status >= 400:
            raise AdapterExecutionError(
                f"image search returned HTTP {response.status}",
                details={"source": self.name, "status": response.status},
            )

        await self._assert_not_blocked(page)

        try:
            await page.wait_for_selector(_RESULT_SELECTOR, timeout=15_000)
        except Exception as exc:  # playwright's TimeoutError lives in playwright.errors
            title = await page.title()
            raise AdapterExecutionError(
                "image search page did not return result cards; the layout may have "
                "changed or the request was throttled",
                details={"source": self.name, "page_title": title},
            ) from exc

        raw_items = await page.eval_on_selector_all(
            _RESULT_SELECTOR,
            "els => els.map(e => e.getAttribute('m')).filter(Boolean)",
        )

        candidates: list[ImageCandidate] = []
        seen: set[str] = set()
        for raw in raw_items:
            candidate = _parse_bing_item(raw)
            if candidate is None or candidate.image_url in seen:
                continue
            seen.add(candidate.image_url)
            candidates.append(candidate)
            if len(candidates) >= limit:
                break

        if not candidates:
            raise AdapterExecutionError(
                "image search returned no usable candidates",
                details={"source": self.name, "query": query},
            )
        return candidates

    async def _assert_not_blocked(self, page: Any) -> None:
        title = (await page.title()) or ""
        lowered = title.lower()
        if any(marker in lowered for marker in _TITLE_MARKERS):
            raise BrowserAccessDeniedError(
                "the search provider is asking for human verification; "
                "open a normal browser, resolve it, then retry",
                details={"source": self.name, "page_title": title},
            )
        try:
            body = (await page.inner_text("body"))[:2000].lower()
        except Exception:
            return
        if any(marker in body for marker in _BODY_MARKERS):
            raise BrowserAccessDeniedError(
                "the search provider is asking for human verification; "
                "open a normal browser, resolve it, then retry",
                details={"source": self.name, "page_title": title},
            )


def _parse_bing_item(raw: str) -> ImageCandidate | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    image_url = payload.get("murl")
    if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
        return None

    width = _positive_int(payload.get("mw"))
    height = _positive_int(payload.get("mh"))
    page_url = payload.get("purl")
    title = payload.get("t")
    thumbnail = payload.get("turl")

    return ImageCandidate(
        image_url=image_url,
        page_url=page_url if isinstance(page_url, str) else None,
        title=title if isinstance(title, str) else None,
        thumbnail_url=thumbnail if isinstance(thumbnail, str) else None,
        width=width,
        height=height,
    )


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


SOURCES: dict[str, ImageSource] = {BingImageSource.name: BingImageSource()}


def get_source(name: str) -> ImageSource:
    """Return the source registered under ``name``."""
    try:
        return SOURCES[name]
    except KeyError as exc:
        raise AdapterExecutionError(
            f"unknown image source {name!r}; available: {sorted(SOURCES)}",
            details={"source": name},
        ) from exc


__all__ = ["SOURCES", "BingImageSource", "ImageCandidate", "ImageSource", "get_source"]
