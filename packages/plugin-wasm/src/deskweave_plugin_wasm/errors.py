"""Errors raised by the WASM plugin host.

These subclass :class:`deskweave_contracts.DeskWeaveError` so they serialise into
the same ``ErrorInfo`` shape every other part of the system uses, and so a caller
that already handles ``DeskWeaveError`` does not need a second code path.

Error codes are stable strings: the desktop UI and the audit log key off them,
so renaming one is a breaking change.
"""

from __future__ import annotations

from typing import Any

from deskweave_contracts import DeskWeaveError


class PluginError(DeskWeaveError):
    """Base class for plugin failures."""

    code = "plugin_error"


class PluginNotFoundError(PluginError):
    """The module or its manifest does not exist."""

    code = "plugin_not_found"


class PluginMalformedError(PluginError):
    """The bytes are not a loadable WebAssembly module, or required exports are missing."""

    code = "plugin_malformed"


class PluginAbiMismatchError(PluginError):
    """The plugin speaks a different interface version than this host."""

    code = "plugin_abi_mismatch"


class PluginInvalidInputError(PluginError):
    """The payload does not satisfy the plugin's declared input schema."""

    code = "plugin_invalid_input"


class PluginInvalidOutputError(PluginError):
    """The plugin returned something that is not a well-formed frame of its schema."""

    code = "plugin_invalid_output"


class PluginTimeoutError(PluginError):
    """The plugin exceeded its wall-clock budget and was interrupted."""

    code = "plugin_timeout"


class PluginTrapError(PluginError):
    """The plugin trapped (unreachable, division by zero, panic, ...)."""

    code = "plugin_trapped"


class PluginResourceError(PluginError):
    """The plugin exhausted a declared resource (fuel or memory)."""

    code = "plugin_resource_exhausted"


class PluginCapabilityUndeclaredError(PluginError):
    """The plugin called a host capability it never declared in its manifest."""

    code = "plugin_capability_undeclared"


class PluginCapabilityDeniedError(PluginError):
    """The capability was declared, but the host policy did not authorise it."""

    code = "plugin_capability_denied"


class PluginHostError(PluginError):
    """The *host* side of a host call failed, so the invocation did not complete.

    Kept distinct from :class:`PluginCapabilityDeniedError` on purpose: "the
    policy said no" and "our own capability raised" are different facts, and
    reporting the second as the first would let a host defect hide behind a
    plugin-policy label forever.
    """

    code = "plugin_host_error"


class PluginIntegrityError(PluginError):
    """The module on disk is not the module the manifest or the pin describes.

    Covers a modified file, a mismatch between manifest and module, a path that
    tries to escape the plugin directory, and a record that no longer matches.
    It says *what changed*, never *who changed it*: a hash is evidence of
    content, not of authorship.
    """

    code = "plugin_integrity"


class PluginInstallError(PluginError):
    """Pinning a plugin failed: version conflict, or the manifest contradicts the file."""

    code = "plugin_install"


class PluginNotApprovedError(PluginError):
    """No approval covers this exact build, so it must not run.

    Deliberately *not* a subclass of :class:`PluginIntegrityError`: a matching
    hash says the bytes are the ones the manifest describes, and says nothing
    about whether a person agreed to let them ask for those capabilities. A host
    that answered "integrity failed" here would blame the file for a missing
    decision.
    """

    code = "plugin_not_approved"


class PluginLifecycleError(PluginError):
    """The requested lifecycle transition is not legal from the current state.

    Carries ``state`` and ``requested`` in ``details`` so the caller can render
    the transition table instead of guessing.
    """

    code = "plugin_lifecycle"


class PluginStoreLockedError(PluginError):
    """Another process already holds this approval store.

    Separate from :class:`PluginLifecycleError` because it is not a transition
    problem: the store is fine, the *process* is the second one. ``details``
    carries ``path`` (the store) and ``lock_path`` (the file that was locked), so
    an operator can tell "two runtimes are pointed at one store" from "the lock
    file cannot be created".
    """

    code = "plugin_store_locked"


def plugin_error(
    cls: type[PluginError],
    message: str,
    *,
    plugin_id: str | None = None,
    **details: Any,
) -> PluginError:
    """Build ``cls`` with the plugin id folded into ``details``."""
    merged: dict[str, Any] = dict(details)
    if plugin_id is not None:
        merged["plugin_id"] = plugin_id
    return cls(message, details=merged or None)


__all__ = [
    "PluginAbiMismatchError",
    "PluginCapabilityDeniedError",
    "PluginCapabilityUndeclaredError",
    "PluginError",
    "PluginHostError",
    "PluginInstallError",
    "PluginIntegrityError",
    "PluginInvalidInputError",
    "PluginInvalidOutputError",
    "PluginLifecycleError",
    "PluginMalformedError",
    "PluginNotApprovedError",
    "PluginNotFoundError",
    "PluginResourceError",
    "PluginStoreLockedError",
    "PluginTimeoutError",
    "PluginTrapError",
    "plugin_error",
]
