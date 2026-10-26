"""The byte-level contract between the host and a WASM plugin.

Why a hand-rolled ABI instead of WIT / the Component Model: ``wasmtime-py``
50.0.1 binds only core WebAssembly. It has no ``Component`` type, no
``component-model`` instantiation path and no WIT support, so a canonical
component ABI cannot be executed by this host at all. Rather than depend on a
binding that does not exist, the prototype pins a *core module* ABI that any
language able to emit ``wasm32-wasip1`` can implement. See
``docs/decisions/0014-wasm-plugin-isolation.md`` for the full comparison and for
the migration conditions back to a canonical ABI.

The ABI is deliberately tiny. Everything crossing the boundary is a length-
prefixed UTF-8 JSON frame, because JSON is the one schema language the whole
workspace already agrees on (adapters validate against JSON Schema today):

    frame = u32 little-endian length  ||  length bytes of UTF-8 JSON

Exports a plugin must provide::

    memory                              linear memory, min 1 page
    dw_abi_version() -> i32             must equal ABI_VERSION
    dw_alloc(len: i32) -> i32           return a pointer the host may write to
    dw_free(ptr: i32, len: i32)         release host-allocated buffers
    dw_invoke(ptr: i32, len: i32) -> i32
                                        read the request frame at ``ptr`` and
                                        return a pointer to a response frame

Imports the host provides (and only these; anything else fails instantiation)::

    env.dw_host_call(req_ptr: i32, req_len: i32, out_ptr: i32, out_cap: i32) -> i32

``dw_host_call`` returns the number of response bytes written into
``out_ptr``, or a negative code from :class:`HostCallStatus`. The plugin owns the
output buffer, so the host never allocates inside the sandbox.
"""

from __future__ import annotations

import struct
from typing import Final

from .errors import PluginInvalidOutputError

#: Bumped only for a breaking change to the export/import list above.
ABI_VERSION: Final[int] = 1

EXPORT_MEMORY: Final[str] = "memory"
EXPORT_ABI_VERSION: Final[str] = "dw_abi_version"
EXPORT_ALLOC: Final[str] = "dw_alloc"
EXPORT_FREE: Final[str] = "dw_free"
EXPORT_INVOKE: Final[str] = "dw_invoke"

IMPORT_MODULE: Final[str] = "env"
IMPORT_HOST_CALL: Final[str] = "dw_host_call"

#: Hard ceiling on any frame, independent of per-plugin limits. A frame larger
#: than this is rejected before the bytes are copied out of linear memory.
MAX_FRAME_BYTES: Final[int] = 1024 * 1024

_LENGTH = struct.Struct("<I")


class HostCallStatus:
    """Negative return codes for ``env.dw_host_call``.

    Positive values are byte counts. Keeping the failure channel numeric means a
    plugin can react to a denial without the host having to allocate anything.
    """

    OK: Final[int] = 0  # unused: success is the byte count
    DENIED_NOT_DECLARED: Final[int] = -1
    DENIED_NO_GRANT: Final[int] = -2
    DENIED_UNKNOWN_CAPABILITY: Final[int] = -3
    INVALID_ARGUMENT: Final[int] = -4
    BUFFER_TOO_SMALL: Final[int] = -5
    HOST_ERROR: Final[int] = -6
    #: The plugin used up its per-invocation host-call allowance. Distinct from
    #: the denial codes on purpose: nothing was refused by policy, the plugin
    #: simply asked too many times, and the invocation must fail as a resource
    #: problem rather than as an authorisation problem.
    BUDGET_EXHAUSTED: Final[int] = -7
    #: The plugin used up its per-invocation *cost* allowance. Separate from
    #: ``BUDGET_EXHAUSTED`` because they measure different things: that one counts
    #: attempts, this one prices them. A capability declares a weight, so 64 calls
    #: to a cheap validator and 64 calls to a render are not the same bill.
    COST_EXHAUSTED: Final[int] = -8
    #: Admission refused: the host is already running as many host calls as it
    #: will run (globally, for this plugin, for this invocation, or in the
    #: blocking pool). Nothing was executed and nothing was charged.
    CONCURRENCY_LIMIT: Final[int] = -9
    #: The operation stopped because someone cancelled it, and the stop was
    #: *confirmed* — the coroutine returned or raised ``CancelledError``.
    CANCELLED: Final[int] = -10
    #: Cancellation was requested but the operation had not stopped by the end of
    #: the grace period. Deliberately distinct from ``CANCELLED``: reporting a
    #: request as a confirmed stop is the mistake this code exists to prevent.
    CANCELLATION_UNCONFIRMED: Final[int] = -11
    #: A per-plugin or per-capability rate limit refused the call. Separate from
    #: ``CONCURRENCY_LIMIT`` because they answer different questions: that one is
    #: "everything is busy right now", this one is "you are calling too often".
    #: Nothing was executed and nothing was charged.
    RATE_LIMITED: Final[int] = -12
    #: The call waited in the admission queue for its bounded maximum and the
    #: queue never had room. Nothing was executed and nothing was charged. A
    #: plugin that sees this was *not* denied by policy — it was unlucky in
    #: contention, and retrying later is a reasonable response.
    QUEUE_TIMEOUT: Final[int] = -13


def encode_frame(payload: bytes) -> bytes:
    """Prefix ``payload`` with its little-endian u32 length."""
    if len(payload) > MAX_FRAME_BYTES:
        raise PluginInvalidOutputError(
            f"frame of {len(payload)} bytes exceeds the {MAX_FRAME_BYTES} byte ceiling",
            details={"bytes": len(payload), "limit": MAX_FRAME_BYTES},
        )
    return _LENGTH.pack(len(payload)) + payload


def frame_length(header: bytes) -> int:
    """Read the u32 length prefix without copying the body.

    The host uses this to size its second read: it never trusts a plugin enough
    to copy a whole buffer before knowing how big the frame claims to be.
    """
    if len(header) < _LENGTH.size:
        raise PluginInvalidOutputError(
            f"frame header is truncated: {len(header)} bytes, need {_LENGTH.size}",
            details={"bytes": len(header)},
        )
    return _LENGTH.unpack_from(header, 0)[0]


def decode_frame(
    read: bytes,
    *,
    max_bytes: int = MAX_FRAME_BYTES,
    what: str = "response",
) -> bytes:
    """Split a framed buffer into its JSON body, enforcing ``max_bytes``.

    ``read`` may be longer than the frame (the host reads a fixed window); only
    the declared length is returned.
    """
    if len(read) < _LENGTH.size:
        raise PluginInvalidOutputError(
            f"plugin {what} frame is truncated: {len(read)} bytes, need at least 4",
            details={"bytes": len(read)},
        )
    length = frame_length(read)
    if length > max_bytes:
        raise PluginInvalidOutputError(
            f"plugin {what} of {length} bytes exceeds the {max_bytes} byte limit",
            details={"bytes": length, "limit": max_bytes},
        )
    end = _LENGTH.size + length
    if len(read) < end:
        raise PluginInvalidOutputError(
            f"plugin {what} declares {length} bytes but only {len(read) - 4} are readable",
            details={"declared": length, "available": len(read) - 4},
        )
    return read[_LENGTH.size : end]


__all__ = [
    "ABI_VERSION",
    "EXPORT_ABI_VERSION",
    "EXPORT_ALLOC",
    "EXPORT_FREE",
    "EXPORT_INVOKE",
    "EXPORT_MEMORY",
    "IMPORT_HOST_CALL",
    "IMPORT_MODULE",
    "MAX_FRAME_BYTES",
    "HostCallStatus",
    "decode_frame",
    "encode_frame",
    "frame_length",
]
