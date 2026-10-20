"""Wikimedia Commons connector.

A second reference-image source, built entirely on the public MediaWiki API. It
exists to answer a question about the architecture rather than to add a feature:
can a new connector be added without touching the runtime's execution path?

What this adapter does *not* do, on purpose:

* it does not download image bytes — the artifact store owns fetching, and with it
  the SSRF guard, the byte cap and the content sniffing;
* it does not write to the database or change task state — the workflow owns that;
* it does not accept a URL, a path or a query language from the caller: the only
  inputs are a search phrase and a count.

The capability shares its name and argument shape with the browser connector
(``collect_reference_images``), so the reference workflow can treat both sources
identically. ``source`` is a ``const`` in the schema: a caller cannot ask this
connector for Bing results, and cannot smuggle extra fields in.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from deskweave_adapter_sdk import Adapter, PermissionChecker, ResourceResolver
from deskweave_contracts import (
    AdapterDescriptor,
    AdapterExecutionError,
    AdapterStatus,
    Capability,
    CollectionRequest,
    OperationRequest,
    PermissionScope,
    ReferenceSource,
    RiskLevel,
    ValidationError,
)

from .api import WikimediaClient

COLLECTION_CAPABILITY = "collect_reference_images"

WIKIMEDIA_CAPABILITIES: list[Capability] = [
    Capability(
        name=COLLECTION_CAPABILITY,
        description=(
            "Search Wikimedia Commons for raster reference images and return candidate "
            "URLs with their licence. Downloads and storage are handled by the runtime."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                # Fixed by the connector: naming another source here is a mistake,
                # not a request this adapter can serve.
                "source": {"type": "string", "enum": ["wikimedia"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        # Reading a public JSON API has no local side effects, so the shared
        # permission policy allows it without a confirmation prompt. A source that
        # needed credentials or POSTed anything would have to declare a higher risk.
        risk=RiskLevel.READ_ONLY,
        scope=PermissionScope.NETWORK,
        requires_confirmation=False,
    ),
]


class WikimediaAdapter(Adapter):
    """Reference-image source backed by the Wikimedia Commons API."""

    def __init__(
        self,
        *,
        adapter_id: str = "wikimedia",
        permission_checker: PermissionChecker | None = None,
        resource_resolver: ResourceResolver | None = None,
        client: WikimediaClient | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Wikimedia Commons",
            version="0.0.1",
            description=(
                "Public-API reference image source (Commons). No browser, no scraping, "
                "no credentials; every result carries its licence."
            ),
            capabilities=WIKIMEDIA_CAPABILITIES,
        )
        super().__init__(
            descriptor,
            permission_checker=permission_checker,
            resource_resolver=resource_resolver,
        )
        self._client = client or WikimediaClient(timeout=timeout_seconds)

    # -- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Nothing to open: the API is stateless and reached per request.

        Marking the adapter connected here is the honest answer — refusing to load
        the connector because the network happens to be down would take the whole
        runtime's capability list hostage.
        """
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        """No resources are held; kept idempotent as the contract requires."""
        self._set_status(AdapterStatus.DISCONNECTED)

    # -- execution --------------------------------------------------------
    async def _run(self, request: OperationRequest) -> dict[str, Any]:
        if request.capability != COLLECTION_CAPABILITY:
            raise AdapterExecutionError(
                f"capability {request.capability!r} is not implemented by the wikimedia connector",
                details={"adapter_id": self.adapter_id},
            )

        try:
            payload = CollectionRequest.model_validate(request.arguments)
        except PydanticValidationError as exc:
            # The SDK already validated against the JSON Schema; this second gate
            # catches schema/model drift instead of letting it reach the API call.
            raise ValidationError(
                f"invalid collection request: {exc.error_count()} problem(s)",
                details={"errors": [error["msg"] for error in exc.errors()]},
            ) from exc

        # ``source`` is optional in the shared contract (the browser connector
        # defaults to bing). Here an *omitted* source means "this connector", but
        # an explicit different value is a caller mistake worth naming.
        requested_source = request.arguments.get("source", ReferenceSource.WIKIMEDIA.value)
        if requested_source != ReferenceSource.WIKIMEDIA.value:
            raise ValidationError(
                f"the wikimedia connector cannot serve source {requested_source!r}",
                details={"allowed": [ReferenceSource.WIKIMEDIA.value]},
            )

        search = await self._client.search(payload.query, payload.limit)
        self._logger.info(
            "wikimedia search for %r returned %d candidate(s) (%d skipped, strategy=%s)",
            payload.query,
            len(search.candidates),
            search.skipped,
            search.strategy,
        )
        return {
            "query": search.query,
            "source": str(requested_source),
            "strategy": search.strategy,
            "skipped": search.skipped,
            "candidates": [candidate.as_dict() for candidate in search.candidates],
        }


__all__ = ["COLLECTION_CAPABILITY", "WIKIMEDIA_CAPABILITIES", "WikimediaAdapter"]
