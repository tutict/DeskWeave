"""Unit tests for the Wikimedia connector's adapter behaviour (no network).

These are the guarantees the SDK's ``execute`` template is supposed to give every
connector: schema validation, permission enforcement, timeouts, structured errors
and an idempotent lifecycle. A fake client stands in for the API so a failure here
is always a connector bug, never a network hiccup.
"""

from __future__ import annotations

import asyncio

import pytest

from deskweave_adapter_sdk import PermissionChecker
from deskweave_adapter_wikimedia import WikimediaAdapter
from deskweave_adapter_wikimedia.api import WikimediaCandidate, WikimediaSearch
from deskweave_contracts import (
    AdapterStatus,
    OperationRequest,
    PermissionDecision,
    PermissionResult,
    RiskLevel,
)
from support.doubles import make_png


def candidate(index: int = 0) -> WikimediaCandidate:
    return WikimediaCandidate(
        image_url=f"https://upload.wikimedia.org/wikipedia/commons/{index}.png",
        page_url=f"https://commons.wikimedia.org/wiki/File:{index}.png",
        title=f"File:{index}.png",
        width=1600,
        height=900,
        license="CC BY-SA 4.0",
        artist="Someone",
        mime="image/png",
    )


class FakeClient:
    """Stands in for :class:`WikimediaClient`; records calls, can fail or stall."""

    def __init__(
        self,
        *,
        candidates: list[WikimediaCandidate] | None = None,
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.candidates = [candidate()] if candidates is None else candidates
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int) -> WikimediaSearch:
        self.calls.append((query, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return WikimediaSearch(
            candidates=self.candidates, query=query, strategy="filetype", skipped=0
        )


class RecordingChecker(PermissionChecker):
    def __init__(self, decision: PermissionDecision) -> None:
        self.decision = decision
        self.requests: list = []

    async def check(self, request):
        self.requests.append(request)
        return PermissionResult(decision=self.decision, reason="test")


def _request(arguments: dict, *, timeout: float | None = 5.0) -> OperationRequest:
    return OperationRequest(
        capability="collect_reference_images",
        arguments=arguments,
        timeout_seconds=timeout,
        reason="unit test",
    )


# -- declaration ----------------------------------------------------------
async def test_capability_is_declared_with_the_shared_name() -> None:
    adapter = WikimediaAdapter(client=FakeClient())
    capabilities = await adapter.get_capabilities()
    assert [capability.name for capability in capabilities] == ["collect_reference_images"]
    capability = capabilities[0]
    assert capability.risk is RiskLevel.READ_ONLY
    assert capability.requires_confirmation is False
    assert capability.parameters_schema["additionalProperties"] is False
    assert capability.parameters_schema["properties"]["source"]["enum"] == ["wikimedia"]


async def test_no_capability_can_run_arbitrary_code() -> None:
    """A connector that grew an "exec" capability would be a security regression."""
    adapter = WikimediaAdapter(client=FakeClient())
    names = {capability.name for capability in await adapter.get_capabilities()}
    assert names == {"collect_reference_images"}


# -- happy path -----------------------------------------------------------
async def test_execute_returns_candidates_with_attribution() -> None:
    client = FakeClient()
    adapter = WikimediaAdapter(client=client)
    result = await adapter.execute(_request({"query": "elf armor", "limit": 3}))

    assert result.ok is True and result.error is None
    assert client.calls == [("elf armor", 3)]
    assert result.data["source"] == "wikimedia"
    assert result.data["strategy"] == "filetype"
    payload = result.data["candidates"][0]
    assert payload["image_url"].startswith("https://upload.wikimedia.org/")
    assert payload["license"] == "CC BY-SA 4.0"
    assert payload["thumbnail_url"] == payload["image_url"]


async def test_an_omitted_source_means_this_connector() -> None:
    adapter = WikimediaAdapter(client=FakeClient())
    result = await adapter.execute(_request({"query": "elf armor"}))
    assert result.ok is True
    assert result.data["source"] == "wikimedia"


async def test_the_connector_refuses_to_serve_another_source() -> None:
    client = FakeClient()
    adapter = WikimediaAdapter(client=client)
    result = await adapter.execute(_request({"query": "elf", "source": "bing"}))
    assert result.ok is False
    assert result.error is not None
    # The schema pins ``source`` to a single value, so the rejection happens before
    # the connector body runs — and before any request goes out.
    assert result.error.code == "validation_error"
    assert result.error.details["capability"] == "collect_reference_images"
    assert client.calls == []


async def test_repeated_calls_are_independent() -> None:
    client = FakeClient()
    adapter = WikimediaAdapter(client=client)
    first = await adapter.execute(_request({"query": "one"}))
    second = await adapter.execute(_request({"query": "two"}))
    assert first.ok and second.ok
    assert client.calls == [("one", 6), ("two", 6)]  # default limit comes from the contract


# -- validation -----------------------------------------------------------
@pytest.mark.parametrize(
    "arguments",
    [
        {},  # query is required
        {"query": ""},  # minLength
        {"query": "elf", "limit": 0},  # minimum
        {"query": "elf", "limit": 999},  # maximum
        {"query": "elf", "url": "https://evil.example/x.png"},  # no URL input, ever
        {"query": "elf", "path": "C:/Windows/win.ini"},  # no path input, ever
    ],
)
async def test_invalid_arguments_are_rejected(arguments: dict) -> None:
    client = FakeClient()
    adapter = WikimediaAdapter(client=client)
    result = await adapter.execute(_request(arguments))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"
    assert client.calls == []


async def test_an_unknown_capability_is_reported() -> None:
    adapter = WikimediaAdapter(client=FakeClient())
    result = await adapter.execute(
        OperationRequest(capability="run_shell", arguments={"cmd": "dir"})
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validation_error"
    assert result.error.details["capability"] == "run_shell"


# -- permissions ----------------------------------------------------------
async def test_read_only_collection_is_allowed_without_a_checker() -> None:
    """The SDK's default is deny-by-default for elevated work, not for reads."""
    adapter = WikimediaAdapter(client=FakeClient())
    assert (await adapter.execute(_request({"query": "elf"}))).ok is True


async def test_a_denying_checker_blocks_the_request_before_any_work() -> None:
    checker = RecordingChecker(PermissionDecision.DENY)
    client = FakeClient()
    adapter = WikimediaAdapter(client=client, permission_checker=checker)
    result = await adapter.execute(_request({"query": "elf"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert client.calls == []


async def test_confirmation_requirement_is_reported_not_silently_skipped() -> None:
    checker = RecordingChecker(PermissionDecision.REQUIRE_CONFIRMATION)
    client = FakeClient()
    adapter = WikimediaAdapter(client=client, permission_checker=checker)
    result = await adapter.execute(_request({"query": "elf"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert result.error.details["decision"] == "require_confirmation"
    assert client.calls == []


async def test_the_permission_request_describes_a_network_read() -> None:
    checker = RecordingChecker(PermissionDecision.ALLOW)
    adapter = WikimediaAdapter(client=FakeClient(), permission_checker=checker)
    await adapter.execute(_request({"query": "elf"}))
    request = checker.requests[0]
    assert request.scope.value == "network"
    assert request.risk is RiskLevel.READ_ONLY
    assert request.requires_confirmation is False


# -- failures -------------------------------------------------------------
async def test_a_timeout_is_reported_as_adapter_timeout() -> None:
    adapter = WikimediaAdapter(client=FakeClient(delay=0.5))
    result = await adapter.execute(_request({"query": "elf"}, timeout=0.05))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_timeout_error"


async def test_a_connection_failure_keeps_its_structured_error() -> None:
    from deskweave_contracts import AdapterConnectionError

    client = FakeClient(error=AdapterConnectionError("api down", details={"status": 503}))
    adapter = WikimediaAdapter(client=client)
    result = await adapter.execute(_request({"query": "elf"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_connection_error"
    assert result.error.details["status"] == 503


async def test_an_unexpected_exception_is_wrapped_not_swallowed() -> None:
    adapter = WikimediaAdapter(client=FakeClient(error=RuntimeError("boom")))
    result = await adapter.execute(_request({"query": "elf"}))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "adapter_execution_error"
    assert "RuntimeError" in result.error.message


# -- lifecycle ------------------------------------------------------------
async def test_lifecycle_is_idempotent() -> None:
    adapter = WikimediaAdapter(client=FakeClient())
    assert await adapter.get_status() is AdapterStatus.DISCOVERED
    await adapter.connect()
    assert await adapter.get_status() is AdapterStatus.CONNECTED
    await adapter.disconnect()
    assert await adapter.get_status() is AdapterStatus.DISCONNECTED
    await adapter.disconnect()  # must not raise
    assert await adapter.get_status() is AdapterStatus.DISCONNECTED


def test_the_iconic_png_fixture_still_parses() -> None:
    """Keeps the shared test fixture honest: it is used as a stand-in image."""
    assert make_png().startswith(b"\x89PNG")
