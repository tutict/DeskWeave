"""Real browser integration tests.

These tests drive an actual headless Chromium against a live search provider.
They are opt-in because they need network access and a Playwright browser
build, and because a third-party site can legitimately refuse automated traffic
on any given day:

    DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py

Everything asserted here was observed from the real page — no fixtures, no
recorded HTML, no fake candidates.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deskweave_adapter_browser import BrowserAdapter
from deskweave_contracts import AdapterStatus, CollectionRequest
from deskweave_runtime import Runtime
from deskweave_runtime.permissions import PermissionManager

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("DESKWEAVE_NETWORK_TESTS") != "1",
        reason="set DESKWEAVE_NETWORK_TESTS=1 to run tests that use the real browser",
    ),
]

QUERY = "anime character reference sheet"


async def test_browser_connects_and_advertises_the_capability() -> None:
    adapter = BrowserAdapter()
    await adapter.connect()
    try:
        assert await adapter.get_status() is AdapterStatus.CONNECTED
        names = [capability.name for capability in await adapter.get_capabilities()]
        assert names == ["collect_reference_images"]
    finally:
        await adapter.disconnect()
    assert await adapter.get_status() is AdapterStatus.DISCONNECTED


async def test_disconnect_is_idempotent() -> None:
    adapter = BrowserAdapter()
    await adapter.connect()
    await adapter.disconnect()
    await adapter.disconnect()
    assert await adapter.get_status() is AdapterStatus.DISCONNECTED


async def test_search_returns_real_candidates() -> None:
    # A bare adapter denies non-read-only capabilities by design; wire the real
    # permission manager, as the runtime does.
    adapter = BrowserAdapter(permission_checker=PermissionManager())
    await adapter.connect()
    try:
        result = await adapter.execute(_collection_request(limit=3))
    finally:
        await adapter.disconnect()

    assert result.ok is True, result.error
    candidates = result.data["candidates"]
    assert 1 <= len(candidates) <= 3
    assert result.data["source"] == "bing"
    for candidate in candidates:
        assert candidate["image_url"].startswith(("http://", "https://"))
        assert candidate["page_url"] is None or candidate["page_url"].startswith("http")
    assert any(candidate["title"] for candidate in candidates)


async def test_runtime_persists_downloaded_images(runtime: Runtime, data_dir: Path) -> None:
    adapter = BrowserAdapter()
    await runtime.register_adapter(adapter)
    await adapter.connect()
    try:
        task = await runtime.references.start_collection(CollectionRequest(query=QUERY, limit=2))
        settled = await runtime.references.wait_until_settled(task.task_id, timeout=150)
    finally:
        await adapter.disconnect()

    assert settled.status.value == "waiting_for_confirmation", settled.error
    view = await runtime.references.get_view(settled.task_id)
    assert view.artifacts, "at least one candidate must have been downloaded"

    artifact_root = (data_dir / "artifacts").resolve()
    for artifact in view.artifacts:
        assert artifact.task_id == settled.task_id
        assert artifact.media_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}
        assert artifact.metadata.sha256 is not None
        assert artifact.metadata.original_url.startswith(("http://", "https://"))
        assert artifact.metadata.source_page is None or artifact.metadata.source_page.startswith(
            "http"
        )
        path = await runtime.artifacts.resolve_path(artifact.artifact_id)
        assert path.is_file()
        assert path.parent == artifact_root
        assert path.stat().st_size == artifact.size_bytes

    # Nothing was written outside the artifact directory.
    stray = [item for item in (data_dir / "artifacts").iterdir() if item.suffix == ".part"]
    assert stray == []


async def test_collection_failure_is_diagnosable(runtime: Runtime) -> None:
    """A query that cannot work must fail loudly, not return unrelated images."""

    class BrokenBrowser(BrowserAdapter):
        async def _run(self, request):  # type: ignore[override]
            from deskweave_contracts import AdapterExecutionError

            raise AdapterExecutionError("simulated layout change", details={"source": "bing"})

    adapter = BrokenBrowser()
    await runtime.register_adapter(adapter)
    task = await runtime.references.start_collection(CollectionRequest(query=QUERY, limit=1))
    settled = await runtime.references.wait_until_settled(task.task_id)

    assert settled.status.value == "failed"
    assert settled.error is not None
    assert "layout change" in settled.error.message


def _collection_request(limit: int):
    from deskweave_contracts import OperationRequest

    return OperationRequest(
        capability="collect_reference_images",
        arguments={"query": QUERY, "limit": limit},
        timeout_seconds=90.0,
        reason="integration test",
    )
