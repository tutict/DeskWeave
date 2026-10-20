"""Browser connector.

Phase 1 implements one real capability: ``collect_reference_images``. It drives a
headless Chromium through Playwright, opens a real image-search results page and
parses the result cards; nothing is hard-coded or mocked.

Design notes:

* the Playwright browser is launched **lazily** on first use and closed by
  :meth:`disconnect`, so registering the adapter costs nothing at startup and the
  runtime can shut it down cleanly;
* parsing lives in :mod:`.sources`, so provider changes do not touch lifecycle code;
* the adapter never downloads images itself — the runtime's artifact store owns
  fetching, because that is where the SSRF/size/type policy lives.
"""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from deskweave_adapter_sdk import Adapter, PermissionChecker, ResourceResolver
from deskweave_contracts import (
    AdapterConnectionError,
    AdapterDescriptor,
    AdapterExecutionError,
    AdapterStatus,
    Capability,
    CollectionRequest,
    OperationRequest,
    PermissionScope,
    RiskLevel,
    ValidationError,
)

from .sources import get_source

BROWSER_CAPABILITIES: list[Capability] = [
    Capability(
        name="collect_reference_images",
        description=(
            "Search the web for reference images and return candidate URLs. "
            "Downloads and storage are handled separately by the runtime."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "source": {"type": "string", "enum": ["bing"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MODERATE,
        scope=PermissionScope.BROWSER_CONTROL,
        requires_confirmation=False,
    ),
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserAdapter(Adapter):
    """Connector to a controlled headless browser session."""

    def __init__(
        self,
        *,
        adapter_id: str = "browser",
        permission_checker: PermissionChecker | None = None,
        resource_resolver: ResourceResolver | None = None,
        headless: bool = True,
        page_timeout_ms: int = 30_000,
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Browser",
            version="0.1.0",
            description="Playwright-based browser connector for reference-image collection.",
            capabilities=BROWSER_CAPABILITIES,
        )
        super().__init__(
            descriptor,
            permission_checker=permission_checker,
            resource_resolver=resource_resolver,
        )
        self._headless = headless
        self._page_timeout_ms = page_timeout_ms
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._session_lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Verify that Playwright and a Chromium build are available.

        The browser itself is launched on first use; this keeps startup cheap and
        avoids an idle Chromium process holding memory for the whole session.
        """
        if find_spec("playwright") is None:
            self._set_status(AdapterStatus.ERROR)
            raise AdapterConnectionError(
                "playwright is not installed; run `uv sync --all-packages`",
                details={"adapter_id": self.adapter_id},
            )

        async with self._session_lock:
            if self._browser is not None:
                self._set_status(AdapterStatus.CONNECTED)
                return
            executable = await self._chromium_path()
            if executable is None or not Path(executable).exists():
                self._set_status(AdapterStatus.ERROR)
                raise AdapterConnectionError(
                    "no Chromium build found for Playwright; run `playwright install chromium`",
                    details={"adapter_id": self.adapter_id},
                )
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        """Close page/context/browser/Playwright. Idempotent and never raises."""
        self._set_status(AdapterStatus.DISCONNECTING)
        async with self._session_lock:
            await self._teardown_locked()
        self._set_status(AdapterStatus.DISCONNECTED)

    async def _teardown_locked(self) -> None:
        """Release session resources. Caller must hold ``_session_lock``."""
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = self._browser = self._playwright = None

        for resource in (context, browser):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception as exc:  # best-effort cleanup must not mask shutdown
                self._logger.warning("browser cleanup failed: %s", exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                self._logger.warning("playwright stop failed: %s", exc)

    # -- execution --------------------------------------------------------
    async def _run(self, request: OperationRequest) -> dict[str, Any]:
        if request.capability != "collect_reference_images":
            raise AdapterExecutionError(
                f"capability {request.capability!r} is not implemented by the browser connector",
                details={"adapter_id": self.adapter_id},
            )

        try:
            payload = CollectionRequest.model_validate(request.arguments)
        except PydanticValidationError as exc:
            # The SDK already validated the JSON Schema; this is a second gate so
            # that a schema/runtime drift cannot let malformed input through.
            raise ValidationError(
                f"invalid collection request: {exc.error_count()} problem(s)",
                details={"errors": [error["msg"] for error in exc.errors()]},
            ) from exc

        source = get_source(payload.source.value)
        page = await self._new_page()
        try:
            candidates = await source.search(page, payload.query, payload.limit)
        finally:
            try:
                await page.close()
            except Exception as exc:
                self._logger.warning("failed to close page: %s", exc)

        return {
            "query": payload.query,
            "source": payload.source.value,
            "candidates": [
                {
                    "image_url": candidate.image_url,
                    "page_url": candidate.page_url,
                    "title": candidate.title,
                    "thumbnail_url": candidate.thumbnail_url,
                    "width": candidate.width,
                    "height": candidate.height,
                }
                for candidate in candidates
            ],
        }

    # -- session ----------------------------------------------------------
    async def _new_page(self) -> Any:
        async with self._session_lock:
            if self._browser is None:
                await self._launch_locked()
        try:
            page = await self._context.new_page()
        except Exception as exc:
            raise AdapterConnectionError(
                f"could not create a browser page: {exc}", details={"adapter_id": self.adapter_id}
            ) from exc
        page.set_default_timeout(self._page_timeout_ms)
        page.set_default_navigation_timeout(self._page_timeout_ms)
        return page

    async def _launch_locked(self) -> None:
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context(
                locale="zh-CN",
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as exc:
            await self._teardown_locked()
            self._set_status(AdapterStatus.ERROR)
            raise AdapterConnectionError(
                f"could not launch Chromium: {type(exc).__name__}: {exc}",
                details={"adapter_id": self.adapter_id},
            ) from exc
        self._set_status(AdapterStatus.CONNECTED)

    @staticmethod
    async def _chromium_path() -> str | None:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            return playwright.chromium.executable_path
        finally:
            await playwright.stop()


__all__ = ["BROWSER_CAPABILITIES", "BrowserAdapter"]
