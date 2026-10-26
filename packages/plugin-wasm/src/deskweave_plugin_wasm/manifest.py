"""Declarative plugin manifest and its resource budget.

A manifest is *data*, not authority. ``requested_capabilities`` is what the
plugin would like to call; whether it may call anything is decided by the host
policy (see :mod:`deskweave_plugin_wasm.host`), and never by this document.

Manifests live next to the module as ``<module>.json``. They are validated with
the same pydantic strictness the rest of the workspace uses (``extra="forbid"``),
so a typo in a field name is a load error rather than a silently ignored line.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .abi import ABI_VERSION

#: Ceilings a manifest may not exceed, regardless of what it asks for. A plugin
#: cannot widen its own budget by declaring a bigger number.
MAX_TIMEOUT_MS = 30_000
MAX_FUEL = 5_000_000_000
MAX_MEMORY_BYTES = 256 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
#: Ceiling on host calls per invocation. Fuel alone does not bound them: a single
#: host call costs the guest very little fuel while costing the host a permission
#: check, a JSON round trip and whatever the capability itself does.
MAX_HOST_CALLS = 1_024
#: Ceiling on the per-invocation cost budget. Cost is a *declared weight* per
#: capability, not a measurement: it exists so that 64 calls to a cheap validator
#: and 64 calls to something expensive are not the same bill.
MAX_COST = 4_096


class ResourceLimits(BaseModel):
    """The budget the host enforces for one invocation.

    Defaults are deliberately small; the example plugin needs a fraction of them.
    """

    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = Field(
        default=2_000,
        ge=1,
        le=MAX_TIMEOUT_MS,
        description="Wall-clock budget, enforced by Wasmtime epoch interruption.",
    )
    fuel: int = Field(
        default=50_000_000,
        ge=1,
        le=MAX_FUEL,
        description="Deterministic instruction budget; bounds pure CPU burn independently of time.",
    )
    max_memory_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=64 * 1024,
        le=MAX_MEMORY_BYTES,
        description="Cap on linear memory; memory.grow past it fails instead of allocating.",
    )
    max_input_bytes: int = Field(default=256 * 1024, ge=1, le=MAX_PAYLOAD_BYTES)
    max_output_bytes: int = Field(default=256 * 1024, ge=1, le=MAX_PAYLOAD_BYTES)
    max_host_calls: int = Field(
        default=64,
        ge=1,
        le=MAX_HOST_CALLS,
        description=(
            "Host calls allowed in one invocation. Deliberately separate from "
            "``fuel``: fuel bounds guest instructions, not the work a granted "
            "capability performs on the plugin's behalf."
        ),
    )
    max_cost: int = Field(
        default=64,
        ge=1,
        le=MAX_COST,
        description=(
            "Cost budget for one invocation, in the units each capability "
            "declares. Orthogonal to ``max_host_calls``: that one counts "
            "attempts, this one prices them. A failed or cancelled call is "
            "charged once and not refunded."
        ),
    )


class WasmPluginManifest(BaseModel):
    """Static description of a WASM plugin."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="Stable identifier; also the audit subject.",
    )
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="0.0.0", max_length=32)
    interface_version: int = Field(
        default=ABI_VERSION,
        description="ABI version the plugin was built against; must match the host.",
    )
    entry: str = Field(default="dw_invoke", description="Exported function the host calls.")
    wasm_path: str | None = Field(
        default=None,
        description=(
            "Module path relative to this manifest's directory. ``None`` keeps the "
            "historical convention (a sibling file with the same stem as the "
            "manifest). Absolute paths, drive letters and '..' components are "
            "rejected at load time rather than normalised into something plausible."
        ),
    )
    wasm_sha256: str | None = Field(
        default=None,
        description=(
            "SHA-256 of the module bytes. Optional for backward compatibility: a "
            "host built with ``require_integrity=True`` refuses a manifest that "
            "omits it. This is content identity — it detects substitution, it does "
            "not prove who published the file."
        ),
    )
    description: str = Field(default="", max_length=1024)
    requested_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Host capabilities the plugin intends to call. A declaration only: "
            "every call is still authorised separately by the host policy."
        ),
    )
    input_schema: dict[str, object] = Field(
        default_factory=dict,
        description="JSON Schema for the request body, validated before the module runs.",
    )
    output_schema: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "JSON Schema for the response body. Validated after the module returns, "
            "so a compromised plugin cannot hand the host a shape it did not promise."
        ),
    )
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @field_validator("requested_capabilities")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for item in value:
            name = item.strip()
            if not name:
                raise ValueError("capability names must not be blank")
            seen.setdefault(name, None)
        return list(seen)

    @field_validator("wasm_sha256")
    @classmethod
    def _check_digest(cls, value: str | None) -> str | None:
        # Lowercase only: an upper-case digest is a different string, and
        # comparing it to a computed lowercase one fails in a way that reads
        # like "the file changed" instead of "the manifest is malformed".
        if value is None:
            return None
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("wasm_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("wasm_path")
    @classmethod
    def _check_path(cls, value: str | None) -> str | None:
        # Only the cheap lexical checks live here; the filesystem checks (symlinks,
        # containment, existence) belong to deskweave_plugin_wasm.integrity, which
        # needs the manifest's directory.
        if value is None:
            return None
        if not value.strip() or "\x00" in value:
            raise ValueError("wasm_path must be a non-empty path")
        if value.startswith(("/", "\\")) or (len(value) > 1 and value[1] == ":"):
            raise ValueError("wasm_path must be relative to the manifest directory")
        if ".." in value.replace("\\", "/").split("/"):
            raise ValueError("wasm_path must not contain '..' components")
        return value

    @property
    def declared_capabilities(self) -> frozenset[str]:
        """The capability names this plugin claims it wants to use."""
        return frozenset(self.requested_capabilities)


__all__ = [
    "MAX_COST",
    "MAX_FUEL",
    "MAX_HOST_CALLS",
    "MAX_MEMORY_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_TIMEOUT_MS",
    "ResourceLimits",
    "WasmPluginManifest",
]
