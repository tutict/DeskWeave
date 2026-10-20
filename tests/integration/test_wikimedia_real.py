"""Real Wikimedia Commons integration tests.

Opt-in, like the other external-dependency suites::

    DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_wikimedia_real.py

They answer the questions a mock cannot: does the documented API still return what
the parser expects, does the licence really come back, and — the one that matters
for the workflow — does a real Commons URL survive the artifact downloader's SSRF
guard, byte cap and content sniffing?

Note on retries: this file retries a *connection* failure a few times, and the
connector deliberately does not. On a machine behind an intercepting HTTPS proxy a
TLS handshake intermittently times out (measured here: roughly one connection in
three, same URL and headers, within one process). Retrying belongs in the test
that wants a stable answer, not in the product, where a silent retry would hide a
real outage from the user.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from deskweave_adapter_wikimedia import (
    WikimediaAdapter,
    WikimediaCandidate,
    WikimediaClient,
    WikimediaSearch,
)
from deskweave_contracts import AdapterConnectionError, OperationRequest
from deskweave_runtime.artifacts.downloader import ImageDownloader, validate_remote_url

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("DESKWEAVE_NETWORK_TESTS"),
        reason="set DESKWEAVE_NETWORK_TESTS=1 to run tests that use the public Wikimedia API",
    ),
]

QUERY = "anime character reference"
RASTER = {"image/png", "image/jpeg", "image/gif", "image/webp"}

#: Bounded, so a real outage still fails the suite instead of hanging it.
ATTEMPTS = 4


async def _retry[T](action: Callable[[], Awaitable[T]], *, attempts: int = ATTEMPTS) -> T:
    """Run ``action``, retrying only *connection* failures.

    Anything the connector classified as an execution error is a real bug and is
    raised immediately: a retry budget must not paper over a broken parser.
    """
    last: AdapterConnectionError | None = None
    for _ in range(attempts):
        try:
            return await action()
        except AdapterConnectionError as exc:  # transport-level, see module docstring
            last = exc
    assert last is not None
    raise AssertionError(
        f"still unreachable after {attempts} attempts: {last.message} "
        f"(details={last.details}) — check the network, not the code"
    )


def _search(query: str, limit: int) -> Awaitable[WikimediaSearch]:
    client = WikimediaClient()
    return _retry(lambda: client.search(query, limit))


def _candidates(query: str, limit: int) -> Awaitable[list[WikimediaCandidate]]:
    async def run() -> list[WikimediaCandidate]:
        return (await _search(query, limit)).candidates

    return run()


def _execute(query: str, limit: int) -> Awaitable[dict]:
    adapter = WikimediaAdapter()

    async def run() -> dict:
        result = await adapter.execute(
            OperationRequest(
                capability="collect_reference_images",
                arguments={"query": query, "limit": limit},
                timeout_seconds=30.0,
            )
        )
        if not result.ok:
            error = result.error
            assert error is not None
            if error.code == AdapterConnectionError.code:
                # ``execute`` converts every exception into a result, so lift the
                # transport failure back into an exception the retry loop can see.
                raise AdapterConnectionError(error.message, details=error.details)
            raise AssertionError(f"the adapter failed: {error}")
        assert result.data is not None
        return result.data

    return _retry(run)


async def test_a_real_search_returns_licensed_candidates() -> None:
    data = await _execute(QUERY, 4)

    candidates = data["candidates"]
    assert candidates, "the public API returned no candidate for a common query"

    first = candidates[0]
    assert first["image_url"].startswith("https://")
    assert first["image_url"].split("/")[2].endswith("wikimedia.org")
    assert first["media_type"] in RASTER
    assert first["width"] and first["height"]
    assert first["license"], "a Commons result without a licence is not usable as a reference"
    # The strategy tells an operator whether the server-side filter was applied.
    assert data["strategy"] in {"filetype", "plain"}


async def test_a_real_candidate_passes_the_artifact_download_policy(tmp_path: Path) -> None:
    """Source and store must agree: the downloader is the only thing writing bytes."""
    candidates = await _candidates(QUERY, 2)
    assert candidates, "no candidate to download"

    target = candidates[0].image_url
    validate_remote_url(target)  # the SSRF policy accepts a real Commons URL

    downloader = ImageDownloader(tmp_path / "artifacts")
    downloaded = await _retry(lambda: downloader.download(target, artifact_id="art_wikimedia_real"))

    assert downloaded.path.exists()
    assert downloaded.media_type in RASTER
    assert downloaded.width and downloaded.height
    assert len(downloaded.sha256) == 64
    # The downloader sniffs the bytes; re-asserting the signature keeps the test
    # honest if the sniffer and the reported media type ever drift apart.
    head = downloaded.path.read_bytes()[:12]
    assert head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or (
        head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    )


async def test_a_nonsense_query_returns_an_empty_result_not_an_exception() -> None:
    search = await _search("zzqx no-such-topic 9876543210", 3)
    assert isinstance(search.candidates, list)
    assert search.strategy in {"filetype", "plain"}
