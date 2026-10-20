"""Unit tests for the Wikimedia Commons client (no network).

The client has three jobs: build a correct, encoded API URL; turn a payload into
candidates without trusting it; and fail with a structured error instead of a
plausible-looking empty result. All three are tested against fixtures here — the
one test that talks to the real API lives in ``tests/integration``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest

from deskweave_adapter_wikimedia.api import (
    ALLOWED_IMAGE_HOSTS,
    MAX_RESPONSE_BYTES,
    THUMBNAIL_WIDTH,
    WikimediaClient,
    build_params,
    build_url,
    parse_response,
)
from deskweave_contracts import AdapterConnectionError, AdapterExecutionError

PNG_URL = "https://upload.wikimedia.org/wikipedia/commons/1/1a/Example.png"
THUMB_URL = "https://thumb.wikimedia.org/wikipedia/commons/thumb/1/1a/Example.png/1600px.png"


def page(
    *,
    title: str = "File:Example.png",
    mime: str = "image/png",
    url: str = PNG_URL,
    thumb: str | None = THUMB_URL,
    width: int | None = 1600,
    height: int | None = 900,
    license: str | None = "CC BY-SA 4.0",
    artist: str | None = '<a href="https://example.org">Someone</a>',
) -> dict:
    imageinfo: dict = {"mime": mime, "width": width, "height": height, "url": url}
    if thumb is not None:
        imageinfo["thumburl"] = thumb
    imageinfo["descriptionurl"] = "https://commons.wikimedia.org/wiki/File:Example.png"
    imageinfo["extmetadata"] = {}
    if license is not None:
        imageinfo["extmetadata"]["LicenseShortName"] = {"value": license}
    if artist is not None:
        imageinfo["extmetadata"]["Artist"] = {"value": artist}
    return {"title": title, "imageinfo": [imageinfo]}


def payload(*pages: dict) -> dict:
    return {"batchcomplete": True, "query": {"pages": list(pages)}}


# -- URL building ---------------------------------------------------------
def test_search_params_ask_for_the_file_namespace_and_a_bounded_thumbnail() -> None:
    params = build_params("elf armor", 4, filetype_only=True)
    assert params["generator"] == "search"
    assert params["gsrnamespace"] == "6"  # File: namespace, not articles
    assert params["gsrsearch"] == "elf armor filetype:bitmap"
    assert params["gsrlimit"] == "4"
    assert params["iiprop"] == "url|size|mime|extmetadata"
    assert params["iiurlwidth"] == str(THUMBNAIL_WIDTH)
    assert params["formatversion"] == "2"


def test_fallback_params_drop_the_server_side_filter() -> None:
    assert build_params("elf armor", 3, filetype_only=False)["gsrsearch"] == "elf armor"


def test_query_is_url_encoded_not_interpolated() -> None:
    url = build_url(build_params("a & b/../c?", 2, filetype_only=False))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["gsrsearch"] == ["a & b/../c?"]
    # The spaces/& must not leak into the path or create extra parameters.
    assert url.count("gsrsearch=") == 1


def test_params_strip_surrounding_whitespace() -> None:
    assert build_params("  cat  ", 1, filetype_only=False)["gsrsearch"] == "cat"


# -- payload parsing ------------------------------------------------------
def test_candidates_carry_url_size_licence_and_page() -> None:
    found, skipped = parse_response(payload(page()), limit=5)
    assert skipped == 0
    candidate = found[0]
    assert candidate.image_url == THUMB_URL  # the bounded thumbnail, not the original
    assert candidate.width == 1600
    assert candidate.license == "CC BY-SA 4.0"
    assert candidate.artist == "Someone"  # HTML stripped
    assert candidate.page_url.startswith("https://commons.wikimedia.org/wiki/")
    assert candidate.mime == "image/png"


def test_metadata_tags_and_entities_are_flattened() -> None:
    found, _ = parse_response(payload(page(artist="<b>Jane</b> &amp; <i>Joe</i>" * 40)), limit=5)
    artist = found[0].artist or ""
    assert "<" not in artist
    assert "&amp;" not in artist
    assert len(artist) <= 200


def test_non_raster_results_are_skipped_and_counted() -> None:
    found, skipped = parse_response(
        payload(page(mime="application/pdf"), page(mime="image/svg+xml")), limit=5
    )
    assert found == []
    assert skipped == 2


def test_a_result_pointing_at_a_foreign_host_is_dropped() -> None:
    found, skipped = parse_response(
        payload(page(thumb="https://evil.example/x.png", url="https://evil.example/x.png")),
        limit=5,
    )
    assert found == [] and skipped == 1


def test_a_plain_http_result_is_dropped() -> None:
    found, skipped = parse_response(
        payload(page(thumb=None, url="http://upload.wikimedia.org/x.png")), limit=5
    )
    assert found == [] and skipped == 1


def test_the_original_url_is_used_when_no_thumbnail_exists() -> None:
    found, _ = parse_response(payload(page(thumb=None)), limit=5)
    assert found[0].image_url == PNG_URL


def test_duplicate_urls_are_collapsed() -> None:
    found, skipped = parse_response(payload(page(), page()), limit=5)
    assert len(found) == 1
    assert skipped == 1


def test_limit_is_respected() -> None:
    pages = [page(title=f"File:{index}.png", thumb=f"{THUMB_URL}?v={index}") for index in range(5)]
    found, _ = parse_response(payload(*pages), limit=2)
    assert len(found) == 2


def test_pages_without_imageinfo_are_skipped() -> None:
    found, skipped = parse_response({"query": {"pages": [{"title": "File:gone.png"}]}}, limit=5)
    assert found == [] and skipped == 1


def test_old_formatversion_uses_a_page_object() -> None:
    """``formatversion=1`` returns pages as a mapping; the parser tolerates both."""
    found, _ = parse_response({"query": {"pages": {"1": page()}}}, limit=5)
    assert len(found) == 1


def test_an_empty_result_is_not_an_error() -> None:
    found, skipped = parse_response({"batchcomplete": True}, limit=5)
    assert (found, skipped) == ([], 0)


def test_allowed_hosts_are_the_wikimedia_upload_hosts() -> None:
    assert {
        "upload.wikimedia.org",
        "thumb.wikimedia.org",
        "commons.wikimedia.org",
    } == ALLOWED_IMAGE_HOSTS


# -- client behaviour -----------------------------------------------------
def _client(responses: list[bytes], *, calls: list[str] | None = None) -> WikimediaClient:
    queue = list(responses)

    def transport(url: str, timeout: float) -> bytes:
        if calls is not None:
            calls.append(url)
        return queue.pop(0)

    return WikimediaClient(transport=transport)


async def test_search_uses_the_filetype_strategy_when_it_works() -> None:
    calls: list[str] = []
    client = _client([json.dumps(payload(page())).encode()], calls=calls)
    result = await client.search("elf", 3)
    assert result.strategy == "filetype"
    assert len(result.candidates) == 1
    assert len(calls) == 1


async def test_search_retries_without_the_filter_when_the_narrow_search_is_empty() -> None:
    calls: list[str] = []
    client = _client(
        [json.dumps({"batchcomplete": True}).encode(), json.dumps(payload(page())).encode()],
        calls=calls,
    )
    result = await client.search("very niche query", 3)
    assert result.strategy == "plain"
    assert len(result.candidates) == 1
    assert len(calls) == 2
    assert "filetype%3Abitmap" in calls[0]
    assert "filetype%3Abitmap" not in calls[1]


async def test_a_fully_filtered_response_triggers_exactly_one_fallback_attempt() -> None:
    """An answer full of PDFs still leaves nothing to show, so the plain search runs once."""
    calls: list[str] = []
    pdf_only = json.dumps(payload(page(mime="application/pdf"))).encode()
    empty = json.dumps({"batchcomplete": True}).encode()
    client = _client([pdf_only, empty], calls=calls)
    result = await client.search("elf", 3)
    assert result.candidates == []
    assert result.skipped == 1  # counted once, not once per attempt
    assert len(calls) == 2


async def test_a_http_error_becomes_a_connection_error() -> None:
    def transport(url: str, timeout: float) -> bytes:
        raise urllib.error.HTTPError(url, 503, "busy", None, None)  # type: ignore[arg-type]

    client = WikimediaClient(transport=transport)
    with pytest.raises(AdapterConnectionError) as excinfo:
        await client.search("elf", 1)
    assert excinfo.value.code == "adapter_connection_error"
    assert excinfo.value.details["status"] == 503


async def test_a_network_failure_becomes_a_connection_error() -> None:
    def transport(url: str, timeout: float) -> bytes:
        raise OSError("connection reset")

    with pytest.raises(AdapterConnectionError):
        await WikimediaClient(transport=transport).search("elf", 1)


async def test_a_non_json_body_is_an_execution_error_not_an_empty_result() -> None:
    with pytest.raises(AdapterExecutionError):
        await WikimediaClient(transport=lambda url, timeout: b"<html>nope</html>").search("elf", 1)


async def test_an_api_error_object_is_surfaced() -> None:
    body = json.dumps({"error": {"code": "gsrsearch-error", "info": "bad query"}}).encode()
    with pytest.raises(AdapterExecutionError) as excinfo:
        await WikimediaClient(transport=lambda url, timeout: body).search("elf", 1)
    assert excinfo.value.details["api_code"] == "gsrsearch-error"


async def test_a_json_array_instead_of_an_object_is_rejected() -> None:
    with pytest.raises(AdapterExecutionError):
        await WikimediaClient(transport=lambda url, timeout: b"[]").search("elf", 1)


def test_the_response_cap_is_a_megabyte_scale_number() -> None:
    # Guards against someone "fixing" a bug by removing the cap entirely.
    assert 100_000 < MAX_RESPONSE_BYTES <= 8 * 1024 * 1024
