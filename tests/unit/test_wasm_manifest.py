"""Unit tests for the plugin manifest contract.

A manifest is data, so the interesting cases are the ones that must be *refused*:
a plugin cannot widen its own resource budget, invent capability names or skip
required identity fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deskweave_plugin_wasm.manifest import (
    MAX_MEMORY_BYTES,
    MAX_TIMEOUT_MS,
    ResourceLimits,
    WasmPluginManifest,
)


def _manifest(**overrides: object) -> WasmPluginManifest:
    base: dict[str, object] = {
        "plugin_id": "reference-metadata",
        "name": "Reference metadata",
    }
    base.update(overrides)
    return WasmPluginManifest.model_validate(base)


def test_defaults_match_the_documented_budget() -> None:
    manifest = _manifest()
    assert manifest.interface_version == 1
    assert manifest.entry == "dw_invoke"
    assert manifest.limits.timeout_ms == 2_000
    assert manifest.limits.max_memory_bytes == 16 * 1024 * 1024
    assert manifest.requested_capabilities == []


def test_plugin_id_must_be_a_stable_slug() -> None:
    for bad in ("Reference Metadata", "UPPER", "-leading", "with/slash", ""):
        with pytest.raises(ValidationError):
            _manifest(plugin_id=bad)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WasmPluginManifest.model_validate({"plugin_id": "x", "name": "X", "grants": ["everything"]})


def test_timeout_cannot_exceed_the_host_ceiling() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(timeout_ms=MAX_TIMEOUT_MS + 1)


def test_memory_cannot_exceed_the_host_ceiling() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(max_memory_bytes=MAX_MEMORY_BYTES * 2)


def test_zero_timeout_is_refused() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(timeout_ms=0)


def test_capability_declarations_are_deduplicated_and_trimmed() -> None:
    manifest = _manifest(requested_capabilities=["a.b", " a.b ", "c.d"])
    assert manifest.requested_capabilities == ["a.b", "c.d"]
    assert manifest.declared_capabilities == frozenset({"a.b", "c.d"})


def test_blank_capability_is_refused() -> None:
    with pytest.raises(ValidationError):
        _manifest(requested_capabilities=["ok", "   "])


def test_declaration_is_not_authority() -> None:
    """The manifest field is named `requested_` on purpose; guard the naming."""
    manifest = _manifest(requested_capabilities=["reference.metadata.validate"])
    assert not hasattr(manifest, "granted_capabilities")
    assert not hasattr(manifest, "allowed_capabilities")


# -- integrity fields (Phase 2.5) --------------------------------------------


def test_a_manifest_without_integrity_fields_still_parses() -> None:
    """Optional keeps every manifest written before Phase 2.5 valid."""
    manifest = _manifest()
    assert manifest.wasm_path is None
    assert manifest.wasm_sha256 is None


def test_a_lowercase_digest_is_accepted() -> None:
    digest = "a" * 64
    manifest = _manifest(wasm_sha256=digest, wasm_path="plugin.wasm")
    assert manifest.wasm_sha256 == digest
    assert manifest.wasm_path == "plugin.wasm"


@pytest.mark.parametrize(
    "digest",
    [
        "A" * 64,  # uppercase: a different string, and a confusing comparison
        "a" * 63,  # too short
        "a" * 65,  # too long
        "g" * 64,  # not hex
        "",
    ],
)
def test_a_malformed_digest_is_refused(digest: str) -> None:
    with pytest.raises(ValidationError):
        _manifest(wasm_sha256=digest)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.wasm",
        "sub/../../escape.wasm",
        "C://Windows//evil.wasm",
        "/etc/passwd",
        "\\\\server\\share\\evil.wasm",
        "",
        "   ",
    ],
)
def test_a_module_path_that_could_escape_is_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        _manifest(wasm_path=path)
