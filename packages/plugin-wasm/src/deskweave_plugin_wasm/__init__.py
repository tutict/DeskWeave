"""WASM/WASI plugin runtime for DeskWeave.

A plugin is a ``wasm32-wasip1`` core module plus a JSON manifest. It runs in a
Wasmtime sandbox with fuel, wall-clock and memory limits, and the only way out is
``env.dw_host_call`` — every call is authorised by the *existing* permission
layer, not by a plugin-specific one.

    from deskweave_plugin_wasm import WasmPluginHost, default_registry

    host = WasmPluginHost(registry=default_registry(permission_manager))
    plugin = host.load("reference_metadata.wasm")
    result = await host.invoke(plugin, {"images": [...]})
    assert result.ok or result.error.code == "plugin_capability_denied"

This example has **no approval step**, which is fine for a host that does not
pin its bytes — and is refused by construction the moment it does:
``WasmPluginHost(require_install_record=True)`` without an ``activation_guard``
raises, because a host that checks the bytes but never asks who approved them
looks strict and is not. Production code builds its host through
:class:`deskweave_plugin_wasm.PluginLifecycle`, which supplies the guard.
"""

from __future__ import annotations

from .abi import ABI_VERSION, MAX_FRAME_BYTES, HostCallStatus
from .errors import (
    PluginAbiMismatchError,
    PluginCapabilityDeniedError,
    PluginCapabilityUndeclaredError,
    PluginError,
    PluginHostError,
    PluginInstallError,
    PluginIntegrityError,
    PluginInvalidInputError,
    PluginInvalidOutputError,
    PluginLifecycleError,
    PluginMalformedError,
    PluginNotApprovedError,
    PluginNotFoundError,
    PluginResourceError,
    PluginStoreLockedError,
    PluginTimeoutError,
    PluginTrapError,
)
from .execution import (
    CallState,
    ExecutionResult,
    ExecutionScope,
    HostExecutor,
    Refusal,
)
from .host import (
    HostCallOutcome,
    HostCapability,
    HostCapabilityRegistry,
    ReferenceMetadataValidate,
    default_registry,
    principal_for,
)
from .integrity import (
    INSTALL_RECORD_NAME,
    InstallRecord,
    install,
    normalise_module_path,
    resolve_module_path,
    sha256_bytes,
    sha256_file,
    verify_install,
)
from .lifecycle import (
    ApprovalCheck,
    ApprovalStore,
    Inspection,
    InstallState,
    JsonApprovalStore,
    LifecycleEvent,
    MemoryApprovalStore,
    PluginApproval,
    PluginLifecycle,
    RevocationReport,
    check_approval,
    discover,
)
from .manifest import MAX_COST, MAX_HOST_CALLS, ResourceLimits, WasmPluginManifest
from .runtime import ActivationGuard, InvocationResult, WasmPlugin, WasmPluginHost

__all__ = [
    "ABI_VERSION",
    "INSTALL_RECORD_NAME",
    "MAX_COST",
    "MAX_FRAME_BYTES",
    "MAX_HOST_CALLS",
    "ActivationGuard",
    "ApprovalCheck",
    "ApprovalStore",
    "CallState",
    "ExecutionResult",
    "ExecutionScope",
    "HostCallOutcome",
    "HostCallStatus",
    "HostCapability",
    "HostCapabilityRegistry",
    "HostExecutor",
    "Inspection",
    "InstallRecord",
    "InstallState",
    "InvocationResult",
    "JsonApprovalStore",
    "LifecycleEvent",
    "MemoryApprovalStore",
    "PluginAbiMismatchError",
    "PluginApproval",
    "PluginCapabilityDeniedError",
    "PluginCapabilityUndeclaredError",
    "PluginError",
    "PluginHostError",
    "PluginInstallError",
    "PluginIntegrityError",
    "PluginInvalidInputError",
    "PluginInvalidOutputError",
    "PluginLifecycle",
    "PluginLifecycleError",
    "PluginMalformedError",
    "PluginNotApprovedError",
    "PluginNotFoundError",
    "PluginResourceError",
    "PluginStoreLockedError",
    "PluginTimeoutError",
    "PluginTrapError",
    "ReferenceMetadataValidate",
    "Refusal",
    "ResourceLimits",
    "RevocationReport",
    "WasmPlugin",
    "WasmPluginHost",
    "WasmPluginManifest",
    "check_approval",
    "default_registry",
    "discover",
    "install",
    "normalise_module_path",
    "principal_for",
    "resolve_module_path",
    "sha256_bytes",
    "sha256_file",
    "verify_install",
]
