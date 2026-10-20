"""Unit tests for the image-search parsers (no browser, no network).

Playwright is replaced by a fake page object, so these tests pin down the
*parsing and error-reporting* contract: a layout change, a captcha wall or an
HTTP error must surface as a specific error, never as an empty-but-successful
result.
"""

from __future__ import annotations

import json

import pytest

from deskweave_adapter_browser.sources import BingImageSource, get_source
from deskweave_contracts import AdapterExecutionError, BrowserAccessDeniedError


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(
        self,
        *,
        title: str = "anime reference - Bing images",
        body: str = "",
        items: list[str] | None = None,
        status: int = 200,
        goto_error: Exception | None = None,
        selector_error: Exception | None = None,
    ) -> None:
        self._title = title
        self._body = body
        self._items = items or []
        self._status = status
        self._goto_error = goto_error
        self._selector_error = selector_error
        self.visited: str | None = None

    async def goto(self, url: str, **kwargs: object) -> FakeResponse:
        self.visited = url
        if self._goto_error is not None:
            raise self._goto_error
        return FakeResponse(self._status)

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return self._body

    async def wait_for_selector(self, selector: str, timeout: int) -> None:
        if self._selector_error is not None:
            raise self._selector_error

    async def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
        return self._items


def card(
    url: str = "https://cdn.example.com/a.jpg",
    *,
    page: str = "https://example.com/post",
    title: str = "A reference",
    thumb: str = "https://cdn.example.com/t.jpg",
    width: object = 800,
    height: object = 600,
) -> str:
    return json.dumps(
        {"murl": url, "purl": page, "t": title, "turl": thumb, "mw": width, "mh": height}
    )


def test_query_is_part_of_the_search_url() -> None:
    url = BingImageSource().build_url("anime girl reference")
    assert "q=anime+girl+reference" in url
    assert url.startswith("https://cn.bing.com/images/search?")


def test_query_with_special_characters_is_encoded() -> None:
    url = BingImageSource().build_url("a&b=c d/e")
    assert "q=a%26b%3Dc+d%2Fe" in url
    assert "&b=" not in url.split("q=", 1)[1].split("&form", 1)[0]


async def test_candidates_are_parsed_from_real_card_shape() -> None:
    page = FakePage(items=[card(), card("https://cdn.example.com/b.jpg", title="Second")])

    candidates = await BingImageSource().search(page, "anime", 5)

    assert [c.image_url for c in candidates] == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.jpg",
    ]
    first = candidates[0]
    assert first.page_url == "https://example.com/post"
    assert first.title == "A reference"
    assert first.thumbnail_url == "https://cdn.example.com/t.jpg"
    assert (first.width, first.height) == (800, 600)


async def test_limit_is_respected_and_duplicates_are_dropped() -> None:
    page = FakePage(
        items=[
            card("https://cdn.example.com/a.jpg"),
            card("https://cdn.example.com/a.jpg"),
            card("https://cdn.example.com/b.jpg"),
            card("https://cdn.example.com/c.jpg"),
        ]
    )

    candidates = await BingImageSource().search(page, "anime", 2)

    assert [c.image_url for c in candidates] == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.jpg",
    ]


async def test_non_http_image_urls_are_skipped() -> None:
    page = FakePage(
        items=[
            json.dumps({"murl": "javascript:alert(1)"}),
            json.dumps({"murl": "data:image/png;base64,AAAA"}),
            card("https://cdn.example.com/ok.jpg"),
        ]
    )

    candidates = await BingImageSource().search(page, "anime", 5)

    assert [c.image_url for c in candidates] == ["https://cdn.example.com/ok.jpg"]


async def test_malformed_card_json_is_skipped() -> None:
    page = FakePage(items=["not json", "[1,2,3]", "null", card()])

    candidates = await BingImageSource().search(page, "anime", 5)

    assert len(candidates) == 1


async def test_changed_layout_reports_an_error() -> None:
    page = FakePage(selector_error=TimeoutError("no selector"))

    with pytest.raises(AdapterExecutionError) as excinfo:
        await BingImageSource().search(page, "anime", 5)

    assert "layout" in excinfo.value.message
    assert excinfo.value.details["source"] == "bing"


async def test_empty_result_set_reports_an_error() -> None:
    page = FakePage(items=[])

    with pytest.raises(AdapterExecutionError) as excinfo:
        await BingImageSource().search(page, "anime", 5)

    assert "no usable candidates" in excinfo.value.message


async def test_http_error_is_reported_with_status() -> None:
    page = FakePage(status=503)

    with pytest.raises(AdapterExecutionError) as excinfo:
        await BingImageSource().search(page, "anime", 5)

    assert excinfo.value.details["status"] == 503


async def test_navigation_failure_is_reported() -> None:
    page = FakePage(goto_error=OSError("net::ERR_NAME_NOT_RESOLVED"))

    with pytest.raises(AdapterExecutionError) as excinfo:
        await BingImageSource().search(page, "anime", 5)

    assert "could not open" in excinfo.value.message
    assert "ERR_NAME_NOT_RESOLVED" in excinfo.value.message


@pytest.mark.parametrize(
    "title",
    ["Attention Required! | Cloudflare", "验证码", "Just a moment...请完成人机验证"],
)
async def test_blocked_page_in_title_is_actionable(title: str) -> None:
    page = FakePage(title=title, items=[card()])

    with pytest.raises(BrowserAccessDeniedError) as excinfo:
        await BingImageSource().search(page, "anime", 5)

    assert excinfo.value.code == "browser_access_denied"
    assert "human verification" in excinfo.value.message


async def test_blocked_page_in_body_is_actionable() -> None:
    page = FakePage(body="Our systems have detected unusual traffic from your network.")

    with pytest.raises(BrowserAccessDeniedError):
        await BingImageSource().search(page, "anime", 5)


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(AdapterExecutionError) as excinfo:
        get_source("google")
    assert "unknown image source" in excinfo.value.message
