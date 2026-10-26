"""Load and run WASM plugins with enforced limits.

The runtime is a *host*, not an adapter. It knows nothing about Blender, browsers
or the artifact store; everything a plugin can reach comes through
:class:`deskweave_plugin_wasm.host.HostCapabilityRegistry`. That keeps the sandbox
boundary in one file and leaves the existing adapters untouched.

Isolation model, concretely:

* one :class:`wasmtime.Engine` per loaded plugin, so epoch-based interruption of
  one plugin cannot interrupt another that happens to be running;
* a **fresh** ``Store`` per invocation — linear memory never survives a call, so
  two invocations cannot leak state into each other;
* fuel metering for deterministic CPU bounds, epoch deadlines for wall-clock
  bounds, and a linear-memory cap enforced by the engine's allocator;
* WASI is defined (Rust's std links against it) but given no preopened
  directories, an empty environment and a size-limited stdout/stderr sink.

What this is *not*: a security boundary against a hostile host, or a sandbox for
native code. See ``docs/architecture/wasm-threat-model.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Self

import wasmtime
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from deskweave_contracts import ErrorInfo

from .abi import (
    ABI_VERSION,
    EXPORT_ABI_VERSION,
    EXPORT_ALLOC,
    EXPORT_FREE,
    EXPORT_MEMORY,
    IMPORT_HOST_CALL,
    IMPORT_MODULE,
    HostCallStatus,
    decode_frame,
    frame_length,
)
from .errors import (
    PluginAbiMismatchError,
    PluginCapabilityDeniedError,
    PluginCapabilityUndeclaredError,
    PluginError,
    PluginHostError,
    PluginIntegrityError,
    PluginInvalidInputError,
    PluginInvalidOutputError,
    PluginLifecycleError,
    PluginMalformedError,
    PluginNotApprovedError,
    PluginNotFoundError,
    PluginResourceError,
    PluginTimeoutError,
    PluginTrapError,
    plugin_error,
)
from .execution import CallState, ExecutionScope, HostExecutor, RevocationBarrier
from .host import (
    DEFAULT_HOST_CALL_TIMEOUT_S,
    HostCallOutcome,
    HostCapabilityRegistry,
    default_registry,
)
from .integrity import (
    MAX_MODULE_BYTES,
    normalise_module_path,
    resolve_module_path,
    sha256_bytes,
    verify_install,
)
from .manifest import WasmPluginManifest

#: How long a single host call may take before the caller stops waiting. The
#: actual enforcement lives in :class:`~deskweave_plugin_wasm.execution.HostExecutor`;
#: this constant is the value the runtime passes down, kept here because tests
#: patch it by name and because it belongs to the runtime's configuration rather
#: than to the executor's defaults.
HOST_CALL_TIMEOUT_S = DEFAULT_HOST_CALL_TIMEOUT_S

#: Bytes of stdout/stderr kept for diagnostics. A plugin that prints in a loop
#: cannot grow host memory through this channel.
STDIO_CAPTURE_BYTES = 8 * 1024

#: Size of one WebAssembly table entry: a function reference, 8 bytes on the
#: hosts this runs on. Used to price tables in the same currency as
#: ``max_memory_bytes`` — see :meth:`WasmPluginHost._instantiate`.
REFERENCE_BYTES = 8

#: Every way Wasmtime can refuse to run guest code.
#:
#: ``Trap`` is **not** a subclass of ``WasmtimeError`` — they are siblings under
#: ``Exception`` — so a guard written as ``except wasmtime.WasmtimeError``
#: silently misses every trap, which is exactly the class of failure a hostile
#: module produces. Both must be listed explicitly wherever guest code is
#: executed, or the trap becomes the one exception that escapes.
WASMTIME_FAILURES: tuple[type[BaseException], ...] = (
    wasmtime.WasmtimeError,
    wasmtime.Trap,
)

#: The ABI, as signatures rather than names. Checked at load time: a module that
#: exports ``dw_invoke`` with the wrong arity is a malformed plugin, not a plugin
#: that fails at call time with a ``TypeError`` from the host's own call frame.
_I32 = "i32"
_ABI_SIGNATURES: dict[str, tuple[list[str], list[str]]] = {
    EXPORT_ABI_VERSION: ([], [_I32]),
    EXPORT_ALLOC: ([_I32], [_I32]),
    EXPORT_FREE: ([_I32, _I32], []),
}
_ENTRY_SIGNATURE: tuple[list[str], list[str]] = ([_I32, _I32], [_I32])


def _guest_write(
    store: wasmtime.Store,
    memory: wasmtime.Memory,
    data: bytes,
    ptr: int,
) -> bool:
    """Write ``data`` at ``ptr`` in guest memory, or report refusal.

    Guest memory is the plugin's, but *where to write* is also the plugin's
    choice — ``dw_alloc`` returns an arbitrary ``i32`` and the host-call output
    pointer comes straight off the guest stack. Both must be validated before
    the write, because ``wasmtime``'s ``Memory.write`` slices Python bytes and
    raises ``IndexError`` for a negative offset and a plain ``IndexError`` for
    one past the end. A Python exception raised inside a host callback is **not**
    contained by Wasmtime — it unwinds out of the guest call, out of ``_run``
    and out of ``invoke`` — so an unchecked write is a way for a plugin to make
    the host raise ``IndexError`` instead of returning a result.
    """
    if ptr < 0 or len(data) == 0:
        return ptr >= 0
    try:
        available = memory.data_len(store)
    except WASMTIME_FAILURES:  # pragma: no cover - detached memory is a host bug
        return False
    if ptr + len(data) > available:
        return False
    try:
        memory.write(store, data, ptr)
    except (IndexError, ValueError, TypeError, *WASMTIME_FAILURES):
        return False
    return True


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of one plugin call, in the same spirit as ``OperationResult``.

    ``ok`` is the only thing a caller should branch on. A failed invocation
    carries a structured :class:`~deskweave_contracts.ErrorInfo` plus audit data,
    and never raises for *expected* failures.
    """

    plugin_id: str
    ok: bool
    data: dict[str, Any] | None
    error: ErrorInfo | None
    duration_ms: float
    fuel_used: int | None
    host_calls: tuple[HostCallOutcome, ...] = ()
    stdout: str = ""
    #: Cost units charged against this invocation's budget, and the budget
    #: itself. Reported even on failure: "how much did the attempt cost" is
    #: exactly what a caller cannot reconstruct from the error alone.
    cost_used: int = 0
    cost_budget: int = 0


@dataclass
class WasmPlugin:
    """A loaded, verified plugin module."""

    manifest: WasmPluginManifest
    engine: wasmtime.Engine
    module: wasmtime.Module
    digest: str
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


#: Called with ``(manifest, digest, records)`` once the bytes have been read and
#: hashed, before the engine instantiates anything. ``records`` is the approval
#: snapshot ``load`` was handed — never the host's own cache. Raising a
#: ``PluginError`` refuses the load. This is where authorisation-to-run is
#: decided, as a separate question from "are these bytes intact" and from "may
#: this call proceed".
ActivationGuard = Callable[[WasmPluginManifest, str, Mapping[str, Any]], None]


class _BoundedSink:
    """Collect at most ``limit`` bytes, then discard the rest."""

    __slots__ = ("_buf", "_limit")

    def __init__(self, limit: int = STDIO_CAPTURE_BYTES) -> None:
        self._buf = bytearray()
        self._limit = limit

    def __call__(self, chunk: bytes) -> None:
        room = self._limit - len(self._buf)
        if room > 0:
            self._buf.extend(chunk[:room])

    def text(self) -> str:
        return bytes(self._buf).decode("utf-8", errors="replace")


class _HostBridge:
    """Synchronous facade over the async capability registry.

    The WASM callback runs on a worker thread, but authorisation lives in the
    async permission layer. Blocking that worker on a coroutine scheduled onto the
    event loop keeps both worlds intact: the loop is free (the caller is awaiting
    the worker), and the plugin sees an ordinary function call.
    """

    __slots__ = (
        "_budget_hit",
        "_calls",
        "_declared",
        "_loop",
        "_max_calls",
        "_outcomes",
        "_plugin_id",
        "_registry",
        "_scope",
        "_timeout_s",
        "_wait_s",
    )

    def __init__(
        self,
        registry: HostCapabilityRegistry,
        *,
        plugin_id: str,
        declared: frozenset[str],
        loop: asyncio.AbstractEventLoop | None,
        max_calls: int,
        scope: ExecutionScope,
        timeout_s: float,
        wait_s: float,
    ) -> None:
        self._registry = registry
        self._loop = loop
        self._plugin_id = plugin_id
        self._declared = declared
        self._max_calls = max_calls
        self._scope = scope
        self._timeout_s = timeout_s
        #: Backstop only. The executor owns the real deadline; this one exists so
        #: a defect that loses the result cannot block the plugin's thread
        #: forever. It must be strictly larger, or this becomes the deadline and
        #: the executor never gets to record *why* it gave up.
        self._wait_s = wait_s
        self._outcomes: list[HostCallOutcome] = []
        self._calls = 0
        self._budget_hit = False

    @property
    def outcomes(self) -> tuple[HostCallOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def scope(self) -> ExecutionScope:
        return self._scope

    @property
    def denials(self) -> tuple[HostCallOutcome, ...]:
        return tuple(o for o in self._outcomes if not o.ok)

    @property
    def budget_hit(self) -> bool:
        return self._budget_hit

    @property
    def max_calls(self) -> int:
        return self._max_calls

    def enter(self) -> bool:
        """Count one host-call *attempt*; return False once the budget is spent.

        Counting attempts rather than dispatches matters: a request the host
        refuses before decoding (bad pointer, malformed JSON, absurd nesting)
        still costs host work and still leaves an audit record, so it has to
        consume budget. Otherwise the cheapest way to exhaust host memory would
        be to send garbage in a loop.
        """
        self._calls += 1
        if self._calls <= self._max_calls:
            return True
        if not self._budget_hit:
            # Recorded once. A plugin that keeps calling after the budget is gone
            # must not be able to grow host memory through the audit trail.
            self._budget_hit = True
            self._outcomes.append(
                HostCallOutcome(
                    capability="<budget>",
                    status=HostCallStatus.BUDGET_EXHAUSTED,
                    decision="budget",
                    reason=f"host call budget of {self._max_calls} exhausted",
                    duration_ms=0.0,
                    state=CallState.REFUSED.value,
                )
            )
        return False

    def reject(
        self, capability: str, status: int, reason: str, *, decision: str = "invalid"
    ) -> None:
        """Record a host call the host refused before dispatching it.

        ``state`` is always ``refused``: these paths never reach the executor, so
        nothing was admitted and nothing ran. It used to be left empty, which made
        "the host refused this request" indistinguishable in the audit from "the
        state was not recorded".
        """
        self._outcomes.append(
            HostCallOutcome(
                capability=capability,
                status=status,
                decision=decision,
                reason=reason,
                duration_ms=0.0,
                state=CallState.REFUSED.value,
            )
        )

    def call(self, capability: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        """Dispatch one host call and return ``(status, json_bytes)``.

        This runs on the plugin's worker thread and every exit path must return
        a status: an exception raised here unwinds through Wasmtime, through
        ``_run`` and out of ``invoke``, turning a plugin failure into a host
        crash.
        """
        coro = self._registry.dispatch(
            plugin_id=self._plugin_id,
            declared=self._declared,
            capability=capability,
            payload=payload,
            scope=self._scope,
            timeout_s=self._timeout_s,
        )

        outcome: HostCallOutcome
        try:
            if self._loop is not None and not self._loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                try:
                    outcome = future.result(timeout=self._wait_s)
                except FutureTimeoutError:
                    # The executor is supposed to have decided and returned by
                    # now, so reaching this means the host lost the result. It is
                    # a *host* failure, recorded as such — and it says out loud
                    # that the work may still be running, because we do not know
                    # that it stopped.
                    future.cancel()
                    outcome = HostCallOutcome(
                        capability=capability,
                        status=HostCallStatus.HOST_ERROR,
                        decision="error",
                        reason=(
                            f"host did not answer within {self._wait_s:.1f}s; "
                            "the operation may still be running"
                        ),
                        duration_ms=self._wait_s * 1000.0,
                        state="timed_out",
                        admitted=True,
                        executed=True,
                        confirmed_stopped=False,
                    )
            else:  # pragma: no cover - only when driven without an event loop
                outcome = asyncio.run(coro)
        except Exception as exc:  # a capability bug must not escape the sandbox
            # A capability, or the registry itself, raised. The plugin gets a
            # status, the audit keeps a record, and no host traceback crosses
            # the boundary.
            logging.getLogger("deskweave.wasm").exception(
                "host capability %s raised while serving plugin %s", capability, self._plugin_id
            )
            outcome = HostCallOutcome(
                capability=capability,
                status=HostCallStatus.HOST_ERROR,
                decision="error",
                reason=f"host call failed with {type(exc).__name__}",
                duration_ms=0.0,
                state="failed",
                admitted=True,
                executed=True,
            )

        self._outcomes.append(outcome)
        if outcome.ok:
            return 0, json.dumps(outcome.data or {}).encode()
        return outcome.status, json.dumps({"error": outcome.reason}).encode()


def _best_effort_free(
    store: wasmtime.Store,
    free: wasmtime.Func,
    ptr: int,
    size: int,
) -> None:
    """Give a buffer back to the plugin, tolerating a sandbox that cannot answer.

    Cleanup runs on paths where the guest has *already* failed, so it must never
    raise: the ``Store`` is discarded as soon as ``_run`` returns, which makes
    the release cosmetic, and letting a secondary trap mask the real failure
    would turn a diagnosable error into a mystery.

    Two details make this less obvious than it reads:

    * ``wasmtime.Trap`` is **not** a subclass of ``wasmtime.WasmtimeError``.
      They are siblings under ``Exception``, so the natural-looking
      ``except WasmtimeError`` catches every error *except* the traps this
      path actually produces.
    * A guest that just ran out of fuel cannot execute ``dw_free`` at all —
      the call traps before the first instruction. Asking anyway costs a
      wasted trap per failure, so the fuel check short-circuits that case.
    """
    if ptr <= 0:
        return
    try:
        if store.get_fuel() <= 0:
            return
    except wasmtime.WasmtimeError:  # pragma: no cover - defensive
        return
    with contextlib.suppress(wasmtime.WasmtimeError, wasmtime.Trap):
        free(store, ptr, size)


class WasmPluginHost:
    """Loads plugins and runs them under a per-plugin resource budget."""

    def __init__(
        self,
        *,
        registry: HostCapabilityRegistry | None = None,
        logger: logging.Logger | None = None,
        executor: HostExecutor | None = None,
        require_integrity: bool = False,
        require_install_record: bool = False,
        activation_guard: ActivationGuard | None = None,
        allow_unguarded: bool = False,
        revocation_barrier: RevocationBarrier | None = None,
    ) -> None:
        """``require_integrity`` turns the manifest's hash into a load condition.

        Default ``False`` keeps every existing caller working: a manifest without
        ``wasm_sha256`` still loads, and the digest is only recorded. A host that
        cares sets one of the two flags and gets a refusal instead of a log line.

        ``require_install_record`` is the stronger mode — the module must match
        the pin written by :func:`deskweave_plugin_wasm.install`, and a manifest
        that has *added* capabilities since it was pinned is refused. Neither
        mode proves who published the file; see ``docs/architecture/plugin-installation.md``.

        ``activation_guard`` answers a third, unrelated question: has a person
        approved *this* build for *these* capabilities? It runs after the bytes
        are hashed and before the engine sees them, so a build whose approval
        does not match is never instantiated. Integrity and authorisation stay
        separate failures on purpose — see ``docs/architecture/plugin-lifecycle.md``.

        A host that pins bytes but never asks who approved them is the exact
        configuration Phase 2.6.1 found in the wild: it *looks* strict, and it
        grants any plugin that happens to match its own pin the right to run.
        Constructing one now raises unless the caller says ``allow_unguarded=True``,
        which is how the integrity layer keeps its own tests without pretending
        they model authorisation.
        """
        if require_install_record and activation_guard is None and not allow_unguarded:
            raise plugin_error(
                PluginLifecycleError,
                "a host that requires an install record must also check an approval: "
                "pass activation_guard=... (use PluginLifecycle.host), or "
                "allow_unguarded=True if this host is deliberately only checking bytes",
                require_install_record=require_install_record,
                activation_guard=False,
            )
        self._registry = registry if registry is not None else default_registry()
        self._logger = logger or logging.getLogger("deskweave.wasm")
        self._plugins: dict[str, WasmPlugin] = {}
        self._require_integrity = require_integrity or require_install_record
        self._require_install_record = require_install_record
        self._activation_guard = activation_guard
        # One executor per host, shared by every plugin it loads: the global
        # concurrency limit is only a limit if it is counted in one place. A
        # registry that already carries an executor keeps it, so a caller can
        # share governance across hosts deliberately.
        if executor is not None:
            self._registry.executor = executor
        self._executor = self._registry.host_executor()
        if revocation_barrier is not None:
            # Every host that acts on the same approval store must block the same
            # plugins: otherwise a revocation committed through one host leaves a
            # queued call admissible in another. Resolved by the lifecycle from
            # the store path; a bare host keeps its own private barrier.
            self._executor.adopt_barrier(revocation_barrier)

    @property
    def executor(self) -> HostExecutor:
        """The executor governing every host call this host makes."""
        return self._executor

    async def aclose(self) -> int:
        """Release execution capacity and report what was still running.

        Returns the number of host operations that had not finished. They cannot
        be stopped (a synchronous capability has no interrupt), so this value is
        the honest answer to "what is left behind" — a caller that logs it can
        answer the question a shutdown usually hides.
        """
        outstanding = await self._executor.aclose()
        if outstanding:
            self._logger.warning("host closed with %d host operation(s) still running", outstanding)
        return outstanding

    # -- loading ----------------------------------------------------------
    def load(
        self,
        module_path: str | Path,
        manifest_path: str | Path | None = None,
        *,
        manifest: WasmPluginManifest | None = None,
        approval: Mapping[str, Any] | None = None,
    ) -> WasmPlugin:
        """Load and verify ``module_path`` together with its manifest.

        Raises :class:`PluginError` subclasses for every expected failure, so the
        caller gets a precise code instead of a generic ``WasmtimeError``.

        **Loading is not authorising.** A host built with an activation guard
        refuses to load unless the caller passes the ``approval`` snapshot the
        guard is asked to judge against, and that snapshot can only be obtained
        by refreshing the approval store — which is what ``PluginLifecycle.activate``
        does under its lock. Without that, ``load`` would answer "may this run?"
        from whatever the host last read, and a build revoked since then would
        be instantiated anyway (Phase 2.6.4 F2: a direct ``host.load`` after a
        revocation in a second lifecycle succeeded).

        ``manifest`` is the *already parsed* manifest to use. ``activate`` reads
        it once and passes it here, so the record it looked up, the manifest the
        guard judges and the bytes that get hashed all come from one snapshot
        (F7). When it is None the file is read here, which is the bare-host path.
        """
        module_file = Path(module_path)
        if not module_file.is_file():
            raise PluginNotFoundError(
                f"plugin module not found: {module_file.name}",
                details={"path": module_file.name},
            )

        manifest_file = Path(manifest_path) if manifest_path else module_file.with_suffix(".json")
        if not manifest_file.is_file():
            raise PluginNotFoundError(
                f"plugin manifest not found: {manifest_file.name}",
                details={"path": manifest_file.name},
            )

        if self._activation_guard is not None and approval is None:
            # Refused here, before a single byte is read: a host that answers to
            # an approval store may not instantiate anything without being told
            # what that store says *now*.
            raise plugin_error(
                PluginNotApprovedError,
                "this host requires a current approval snapshot to load a plugin; "
                "call PluginLifecycle.activate(), which refreshes the record first",
                state="not_approved",
            )

        if manifest is not None:
            raw_manifest = None
        else:
            try:
                raw_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise PluginMalformedError(
                    f"plugin manifest {manifest_file.name} is not valid JSON",
                    details={"path": manifest_file.name},
                ) from exc

        if manifest is None:
            try:
                manifest = WasmPluginManifest.model_validate(raw_manifest)
            except PydanticValidationError as exc:
                raise PluginMalformedError(
                    f"invalid plugin manifest {manifest_file.name}: {exc.error_count()} problem(s)",
                    details={
                        "path": manifest_file.name,
                        "problems": [str(e["msg"]) for e in exc.errors()[:5]],
                    },
                ) from exc

        if manifest.interface_version != ABI_VERSION:
            raise plugin_error(
                PluginAbiMismatchError,
                f"plugin targets interface version {manifest.interface_version}, "
                f"host speaks {ABI_VERSION}",
                plugin_id=manifest.plugin_id,
                expected=ABI_VERSION,
                found=manifest.interface_version,
            )

        if manifest.wasm_path is not None:
            declared_name = normalise_module_path(manifest.wasm_path, plugin_id=manifest.plugin_id)
            if (manifest_file.parent / declared_name).is_file():
                # The manifest sits next to the module it names: the two must agree
                # exactly, and the resolved path may not escape the directory.
                declared_file = resolve_module_path(
                    manifest_file.parent, declared_name, plugin_id=manifest.plugin_id
                )
                if declared_file != module_file.resolve():
                    raise plugin_error(
                        PluginIntegrityError,
                        "the manifest's wasm_path points at a different file than "
                        "the one being loaded",
                        plugin_id=manifest.plugin_id,
                        path=declared_name,
                    )
            elif PurePosixPath(declared_name).name != module_file.name:
                # A host may keep its own copy of the manifest elsewhere. Then the
                # loader's module wins, but the manifest still may not claim a
                # differently named module — that is the contradiction worth
                # refusing, and it is cheap to check.
                raise plugin_error(
                    PluginIntegrityError,
                    "the manifest's wasm_path names a different module than the one being loaded",
                    plugin_id=manifest.plugin_id,
                    path=declared_name,
                    found=module_file.name,
                )

        if self._require_install_record:
            # ``verify_install`` returns the exact bytes it hashed. Parsing those
            # bytes — instead of re-reading the file — is what closes the gap
            # between "the hash was checked" and "the module that runs".
            _record, data = verify_install(manifest_file)
        else:
            data = module_file.read_bytes()
        if len(data) > MAX_MODULE_BYTES:
            raise plugin_error(
                PluginIntegrityError,
                f"plugin module is {len(data)} bytes, above the {MAX_MODULE_BYTES} byte ceiling",
                plugin_id=manifest.plugin_id,
                path=module_file.name,
            )
        digest = sha256_bytes(data)

        if self._require_integrity:
            if manifest.wasm_sha256 is None:
                raise plugin_error(
                    PluginIntegrityError,
                    "this host requires integrity, but the manifest declares no wasm_sha256",
                    plugin_id=manifest.plugin_id,
                    path=manifest_file.name,
                )
            if manifest.wasm_sha256 != digest:
                raise plugin_error(
                    PluginIntegrityError,
                    "the plugin module does not match the hash in its manifest",
                    plugin_id=manifest.plugin_id,
                    path=module_file.name,
                    expected=manifest.wasm_sha256,
                    found=digest,
                )

        if self._activation_guard is not None:
            # Before the engine. A build nobody approved must not be instantiated
            # at all, so that "loaded" keeps meaning "verified and permitted".
            # ``approval`` is not None here: load() refused above otherwise.
            self._activation_guard(manifest, digest, approval)  # type: ignore[arg-type]

        config = wasmtime.Config()
        config.consume_fuel = True
        config.epoch_interruption = True
        engine = wasmtime.Engine(config)

        try:
            module = wasmtime.Module(engine, data)
        except WASMTIME_FAILURES as exc:
            raise plugin_error(
                PluginMalformedError,
                f"module is not loadable WebAssembly: {exc}",
                plugin_id=manifest.plugin_id,
                digest=digest,
            ) from exc

        self._check_exports(module, manifest)

        plugin = WasmPlugin(
            manifest=manifest,
            engine=engine,
            module=module,
            digest=digest,
            path=module_file,
        )
        self._verify_abi_version(plugin)
        self._plugins[manifest.plugin_id] = plugin
        self._logger.info(
            "loaded wasm plugin %s v%s (%d bytes, sha256=%s)",
            manifest.plugin_id,
            manifest.version,
            len(data),
            digest[:12],
        )
        return plugin

    def get(self, plugin_id: str) -> WasmPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as exc:
            raise PluginNotFoundError(
                f"plugin {plugin_id!r} is not loaded",
                details={"plugin_id": plugin_id},
            ) from exc

    def _check_exports(self, module: wasmtime.Module, manifest: WasmPluginManifest) -> None:
        present = {export.name: export for export in module.exports}
        required = {EXPORT_MEMORY, EXPORT_ABI_VERSION, EXPORT_ALLOC, EXPORT_FREE, manifest.entry}
        missing = sorted(required - set(present))
        if missing:
            raise plugin_error(
                PluginMalformedError,
                f"module is missing required export(s): {', '.join(missing)}",
                plugin_id=manifest.plugin_id,
                missing=missing,
            )

        # Names are not enough. Calling a zero-argument ``dw_alloc`` with one
        # argument raises out of the host's own call frame, and a ``dw_invoke``
        # with no result makes ``int()`` raise ``TypeError`` — both look like a
        # host bug and neither is catchable as a plugin error if it reaches the
        # call site. Reject the mismatch here, where it is plainly a malformed
        # plugin, using the module's declared types (no instantiation needed).
        expected = dict(_ABI_SIGNATURES)
        expected[manifest.entry] = _ENTRY_SIGNATURE
        for name, (params, results) in expected.items():
            export = present.get(name)
            if export is None:
                continue
            declared = export.type
            if not isinstance(declared, wasmtime.FuncType):
                raise plugin_error(
                    PluginMalformedError,
                    f"export {name!r} is not a function",
                    plugin_id=manifest.plugin_id,
                    export=name,
                )
            found = ([str(t) for t in declared.params], [str(t) for t in declared.results])
            if found != (params, results):
                raise plugin_error(
                    PluginMalformedError,
                    f"export {name!r} has signature ({', '.join(found[0])}) -> "
                    f"({', '.join(found[1])}), the ABI requires "
                    f"({', '.join(params)}) -> ({', '.join(results)})",
                    plugin_id=manifest.plugin_id,
                    export=name,
                    expected={"params": params, "results": results},
                    found={"params": found[0], "results": found[1]},
                )

    def _verify_abi_version(self, plugin: WasmPlugin) -> None:
        """Instantiate once and ask the module which ABI it was built against.

        Instantiation runs guest code (``start`` sections, data-segment
        initialisers, global init expressions), and ``dw_abi_version`` is guest
        code too. Either can trap, and a trap is not a ``WasmtimeError`` — this
        is the load-time twin of the failure that used to escape ``_run``.
        """
        try:
            store, exports = self._instantiate(plugin)
            reported = int(exports[EXPORT_ABI_VERSION](store))
        except WASMTIME_FAILURES as exc:
            raise plugin_error(
                PluginMalformedError,
                f"module cannot be instantiated: {exc}",
                plugin_id=plugin.manifest.plugin_id,
            ) from exc
        if reported != ABI_VERSION:
            raise plugin_error(
                PluginAbiMismatchError,
                f"module reports interface version {reported}, host speaks {ABI_VERSION}",
                plugin_id=plugin.manifest.plugin_id,
                expected=ABI_VERSION,
                found=reported,
            )

    def _instantiate(
        self,
        plugin: WasmPlugin,
        *,
        bridge: _HostBridge | None = None,
        sink: _BoundedSink | None = None,
    ) -> tuple[wasmtime.Store, dict[str, Any]]:
        limits = plugin.manifest.limits
        store = wasmtime.Store(plugin.engine)
        store.set_fuel(limits.fuel)
        # ``set_limits`` leaves every field it is not given **unlimited**, so
        # naming only ``memory_size`` bounds linear memory and nothing else. A
        # module can declare ``(table 20000000 funcref)`` — 160 MB of references
        # — reach no ``memory.grow`` at all, and stay inside a 1 MiB memory cap.
        # The threat model calls memory exhaustion "verified"; with one field set
        # that claim was about pages, not about memory.
        #
        # Tables are therefore priced in the same currency as pages: one entry is
        # one reference, and the budget that buys linear memory buys table slots.
        # ``memories``/``tables``/``instances`` are pinned to one because
        # ``memory_size`` is a *per-memory* allowance — leaving the count open
        # multiplies the budget by however many memories a module declares. A
        # wasm32-wasip1 module needs exactly one of each.
        table_elements = max(1, limits.max_memory_bytes // REFERENCE_BYTES)
        store.set_limits(
            memory_size=limits.max_memory_bytes,
            table_elements=table_elements,
            memories=1,
            tables=1,
            instances=1,
        )
        # Epoch interruption is armed at store creation, and its default deadline
        # is the *current* epoch — which traps at the first check. Give it one
        # tick of headroom so instantiation and a normal call are unaffected while
        # a single watchdog increment still interrupts the plugin.
        store.set_epoch_deadline(1)

        capture = sink or _BoundedSink()
        wasi = wasmtime.WasiConfig()
        wasi.argv = []
        wasi.env = []
        # No preopen_dir: the plugin has no filesystem view at all. Output goes to
        # a size-limited sink, so a chatty plugin cannot grow host memory and
        # nothing it prints reaches the host's real stdout.
        wasi.stdout_custom = capture
        wasi.stderr_custom = capture
        store.set_wasi(wasi)

        linker = wasmtime.Linker(plugin.engine)
        linker.define_wasi()
        self._define_host_call(linker, plugin, store, bridge)

        instance = linker.instantiate(store, plugin.module)
        return store, instance.exports(store)

    def _define_host_call(
        self,
        linker: wasmtime.Linker,
        plugin: WasmPlugin,
        store: wasmtime.Store,
        bridge: _HostBridge | None,
    ) -> None:
        i32 = wasmtime.ValType.i32()
        signature = wasmtime.FuncType([i32, i32, i32, i32], [i32])
        max_in = plugin.manifest.limits.max_input_bytes
        max_out = plugin.manifest.limits.max_output_bytes

        def host_call(
            caller: wasmtime.Caller,
            req_ptr: int,
            req_len: int,
            out_ptr: int,
            out_cap: int,
        ) -> int:
            if bridge is None:
                return HostCallStatus.HOST_ERROR

            # Every attempt counts against the budget, including the ones refused
            # below: a refusal still costs host work and still leaves a record.
            if not bridge.enter():
                return HostCallStatus.BUDGET_EXHAUSTED

            def refuse(status: int, reason: str, capability: str = "<undecodable>") -> int:
                # A refusal the plugin is allowed to ignore is a refusal the
                # plugin can hide. Recording it makes the invocation fail even if
                # the guest answers with a happy payload.
                bridge.reject(capability, status, reason)
                return status

            if req_len <= 0 or req_len > max_in or out_cap <= 0:
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    f"request length {req_len} or capacity {out_cap} outside the allowed range",
                )
            memory = caller.get(EXPORT_MEMORY)
            if memory is None:  # pragma: no cover - checked at load time
                return refuse(HostCallStatus.INVALID_ARGUMENT, "module exports no memory")
            try:
                raw = bytes(memory.read(store, req_ptr, req_ptr + req_len))
            except (IndexError, ValueError, TypeError, *WASMTIME_FAILURES):
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    "request pointer is outside linear memory",
                )
            if len(raw) != req_len:
                # ``Memory.read`` clamps instead of raising, so a short read means
                # the plugin pointed outside its own linear memory. Refuse rather
                # than decode a truncated document.
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    f"request of {req_len} bytes read back as {len(raw)}",
                )
            try:
                request = json.loads(raw)
            except (ValueError, TypeError, RecursionError) as exc:
                # ``RecursionError`` is deliberate: a deeply nested document makes
                # the *decoder* recurse, and it is not a ``ValueError``. Left
                # uncaught it unwinds out of the callback, the guest call and
                # ``invoke`` itself.
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    f"request is not decodable JSON ({type(exc).__name__})",
                )
            if not isinstance(request, dict):
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    f"request must be a JSON object, got {type(request).__name__}",
                )

            capability = request.get("capability")
            payload = request.get("payload")
            if not isinstance(capability, str) or not isinstance(payload, dict):
                return refuse(
                    HostCallStatus.INVALID_ARGUMENT,
                    "request must carry a string 'capability' and an object 'payload'",
                )

            status, body = bridge.call(capability, payload)
            if status < 0:
                return status
            if len(body) > min(out_cap, max_out):
                return refuse(
                    status=HostCallStatus.BUFFER_TOO_SMALL,
                    reason=f"response of {len(body)} bytes does not fit capacity {out_cap}",
                    capability=capability,
                )
            if not _guest_write(store, memory, body, out_ptr):
                # The plugin chose an output buffer that does not exist. It gets a
                # status, not an ``IndexError`` from the host's stack frame.
                return refuse(
                    status=HostCallStatus.BUFFER_TOO_SMALL,
                    reason=f"output buffer at {out_ptr} (+{len(body)}) is outside linear memory",
                    capability=capability,
                )
            return len(body)

        linker.define_func(
            IMPORT_MODULE,
            IMPORT_HOST_CALL,
            signature,
            host_call,
            access_caller=True,
        )

    # -- invocation -------------------------------------------------------
    async def invoke(self, plugin: WasmPlugin, payload: dict[str, Any]) -> InvocationResult:
        """Run ``plugin`` on ``payload`` and report the outcome without raising."""
        started = time.perf_counter()
        manifest = plugin.manifest

        try:
            request_bytes = self._prepare_payload(manifest, payload)
        except PluginError as exc:
            return self._failed(manifest.plugin_id, exc, started)

        loop = asyncio.get_running_loop()
        limits = manifest.limits
        scope = ExecutionScope(plugin_id=manifest.plugin_id, max_cost=limits.max_cost)
        bridge = _HostBridge(
            self._registry,
            plugin_id=manifest.plugin_id,
            declared=manifest.declared_capabilities,
            loop=loop,
            max_calls=limits.max_host_calls,
            scope=scope,
            timeout_s=HOST_CALL_TIMEOUT_S,
            wait_s=HOST_CALL_TIMEOUT_S + self._executor.cancellation_grace_s + 1.0,
        )
        sink = _BoundedSink()

        async with plugin.lock:
            try:
                data, fuel_used = await asyncio.to_thread(
                    self._run, plugin, request_bytes, bridge, sink
                )
            except PluginError as exc:
                # A refusal outranks whatever the plugin did with it: if the host
                # refused or failed a call, that is the reason to report, not the
                # mangled response the plugin produced afterwards.
                denial = self._first_denial(bridge, manifest.plugin_id)
                return self._failed(
                    manifest.plugin_id, denial or exc, started, bridge=bridge, sink=sink
                )
            except asyncio.CancelledError:
                # Cancellation is propagated in two directions:
                #  * into the guest, by bumping the epoch so the sandboxed code
                #    traps at its next check rather than burning its fuel budget;
                #  * into the host call, by cancelling the capability's task.
                # The worker thread is blocked in ``future.result()`` inside the
                # guest callback, so without the second step the thread would sit
                # there until the host-call deadline even though nobody wants the
                # answer. Synchronous capabilities cannot be interrupted at all —
                # ``abort_scope`` returns how many it could ask, not a promise.
                asked = self._executor.abort_scope(scope)
                plugin.engine.increment_epoch()
                self._logger.warning(
                    "plugin %s cancelled during invocation (%d host call(s) asked to stop)",
                    manifest.plugin_id,
                    asked,
                )
                raise
            except Exception as exc:  # containment net: see the comment below
                # Last line of defence. Everything reachable from guest bytes is
                # supposed to be translated before it gets here, and when it is
                # not, this must still be a failed *invocation* rather than an
                # exception thrown at whoever called ``invoke``. It is logged at
                # error level with a traceback precisely because reaching it is a
                # host defect worth fixing, not a normal outcome.
                self._logger.exception(
                    "plugin %s hit an uncontained host failure", manifest.plugin_id
                )
                return self._failed(
                    manifest.plugin_id,
                    plugin_error(
                        PluginTrapError,
                        f"plugin invocation failed inside the host: {type(exc).__name__}",
                        plugin_id=manifest.plugin_id,
                        host_exception=type(exc).__name__,
                    ),
                    started,
                    bridge=bridge,
                    sink=sink,
                )

        # A plugin that swallowed a denial and returned a happy payload must not
        # be reported as a success: the host saw an unauthorised call, so the
        # invocation is failed regardless of what the plugin claims.
        denial = self._first_denial(bridge, manifest.plugin_id)
        if denial is not None:
            return self._failed(manifest.plugin_id, denial, started, bridge=bridge, sink=sink)

        return InvocationResult(
            plugin_id=manifest.plugin_id,
            ok=True,
            data=data,
            error=None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            fuel_used=fuel_used,
            host_calls=bridge.outcomes,
            stdout=sink.text(),
            cost_used=scope.cost_spent,
            cost_budget=scope.max_cost,
        )

    @staticmethod
    def _first_denial(bridge: _HostBridge, plugin_id: str) -> PluginError | None:
        """Translate the first unsuccessful host call into the right error.

        Classifying on ``status`` rather than on the outcome's ``decision``
        string keeps the mapping exhaustive: an exhausted budget is a resource
        problem, a capability that raised is a *host* problem, and only the
        remaining codes are statements about authorisation.

        The error *classes* are deliberately unchanged from Phase 2.3 — the
        public taxonomy is part of the contract. What Phase 2.4 adds is
        ``details["state"]``, so "we refused to admit it", "it timed out" and "it
        was cancelled" stop looking identical to whoever reads the failure.
        """
        denials = bridge.denials
        if not denials:
            return None
        first = denials[0]
        if first.status in (
            HostCallStatus.BUDGET_EXHAUSTED,
            HostCallStatus.COST_EXHAUSTED,
            HostCallStatus.CONCURRENCY_LIMIT,
            HostCallStatus.QUEUE_TIMEOUT,
            HostCallStatus.RATE_LIMITED,
        ):
            return plugin_error(
                PluginResourceError,
                f"plugin exceeded a host resource budget: {first.reason}",
                plugin_id=plugin_id,
                capability=first.capability,
                budget=bridge.max_calls,
                cost_budget=bridge.scope.max_cost,
                cost_spent=bridge.scope.cost_spent,
                state=first.state,
            )
        if first.status == HostCallStatus.HOST_ERROR:
            return plugin_error(
                PluginHostError,
                f"host failed to serve {first.capability!r}: {first.reason}",
                plugin_id=plugin_id,
                capability=first.capability,
                reason=first.reason,
                state=first.state,
                confirmed_stopped=first.confirmed_stopped,
            )
        if first.status in (HostCallStatus.CANCELLED, HostCallStatus.CANCELLATION_UNCONFIRMED):
            # Cancellation is not a policy refusal and not the plugin's fault.
            # ``confirmed_stopped`` is carried through verbatim: an unconfirmed
            # request must not be reported as a stop.
            return plugin_error(
                PluginHostError,
                f"host call to {first.capability!r} was cancelled: {first.reason}",
                plugin_id=plugin_id,
                capability=first.capability,
                state=first.state,
                confirmed_stopped=first.confirmed_stopped,
            )
        if first.status in (HostCallStatus.INVALID_ARGUMENT, HostCallStatus.BUFFER_TOO_SMALL):
            # The plugin's own request was malformed: wrong pointers, a body that
            # does not fit the buffer it offered, or a payload the capability's
            # schema rejects. Not an authorisation decision, so it must not be
            # reported as one.
            return plugin_error(
                PluginInvalidInputError,
                f"malformed host call to {first.capability!r}: {first.reason}",
                plugin_id=plugin_id,
                capability=first.capability,
                status=first.status,
                state=first.state,
            )
        cls = (
            PluginCapabilityUndeclaredError
            if first.status == HostCallStatus.DENIED_NOT_DECLARED
            else PluginCapabilityDeniedError
        )
        return plugin_error(
            cls,
            f"host capability {first.capability!r} was not authorised: {first.reason}",
            plugin_id=plugin_id,
            capability=first.capability,
            decision=first.decision,
            denied_calls=len(denials),
        )

    def _prepare_payload(self, manifest: WasmPluginManifest, payload: dict[str, Any]) -> bytes:
        if manifest.input_schema:
            try:
                Draft202012Validator(dict(manifest.input_schema)).validate(payload)
            except JsonSchemaValidationError as exc:
                raise plugin_error(
                    PluginInvalidInputError,
                    f"input does not match the plugin schema: {exc.message}",
                    plugin_id=manifest.plugin_id,
                    path=list(exc.absolute_path),
                ) from exc
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > manifest.limits.max_input_bytes:
            raise plugin_error(
                PluginInvalidInputError,
                f"input of {len(body)} bytes exceeds the {manifest.limits.max_input_bytes} limit",
                plugin_id=manifest.plugin_id,
                bytes=len(body),
            )
        return body

    def _run(
        self,
        plugin: WasmPlugin,
        payload: bytes,
        bridge: _HostBridge,
        sink: _BoundedSink,
    ) -> tuple[dict[str, Any], int]:
        """The blocking half: instantiate, call, read the frame back.

        Runs on a worker thread. Every exit path either returns a dict or raises a
        :class:`PluginError`; no raw Wasmtime exception escapes.
        """
        manifest = plugin.manifest
        limits = manifest.limits

        try:
            store, exports = self._instantiate(plugin, bridge=bridge, sink=sink)
        except WASMTIME_FAILURES as exc:
            raise PluginMalformedError(
                f"module failed to instantiate: {exc}",
                details={"plugin_id": manifest.plugin_id},
            ) from exc

        store.set_epoch_deadline(1)
        memory = exports[EXPORT_MEMORY]
        alloc = exports[EXPORT_ALLOC]
        free = exports[EXPORT_FREE]
        entry = exports[manifest.entry]

        try:
            ptr = int(alloc(store, len(payload)))
        except wasmtime.Trap as exc:
            raise self._translate_trap(manifest.plugin_id, exc, timed_out=False) from exc
        except wasmtime.WasmtimeError as exc:
            raise PluginTrapError(
                f"plugin failed to allocate the request buffer: {exc}",
                details={"plugin_id": manifest.plugin_id},
            ) from exc
        if ptr <= 0:
            raise PluginResourceError(
                "plugin failed to allocate the request buffer",
                details={"plugin_id": manifest.plugin_id},
            )
        if not _guest_write(store, memory, payload, ptr):
            # ``dw_alloc`` returned an address outside linear memory. This is
            # reachable with no grant and no host call, so it must be a plugin
            # error rather than an ``IndexError`` from the host.
            raise PluginResourceError(
                "plugin's dw_alloc returned a pointer outside linear memory",
                details={"plugin_id": manifest.plugin_id, "ptr": ptr, "bytes": len(payload)},
            )

        fired = threading.Event()

        def _deadline() -> None:
            fired.set()
            plugin.engine.increment_epoch()

        watchdog = threading.Timer(limits.timeout_ms / 1000.0, _deadline)
        watchdog.daemon = True
        watchdog.start()
        result_ptr = 0
        try:
            result_ptr = int(entry(store, ptr, len(payload)))
        except wasmtime.ExitTrap as exc:
            raise PluginTrapError(
                f"plugin called proc_exit({exc.code})",
                details={"plugin_id": manifest.plugin_id, "exit_code": exc.code},
            ) from exc
        except wasmtime.Trap as exc:
            raise self._translate_trap(manifest.plugin_id, exc, timed_out=fired.is_set()) from exc
        except wasmtime.WasmtimeError as exc:
            raise PluginTrapError(
                f"plugin execution failed: {exc}",
                details={"plugin_id": manifest.plugin_id},
            ) from exc
        finally:
            watchdog.cancel()
            _best_effort_free(store, free, ptr, len(payload))

        if fired.is_set():
            # The call returned anyway; treat a late result as a timeout so a
            # plugin cannot win a race against its own budget.
            raise PluginTimeoutError(
                "plugin exceeded its wall-clock budget",
                details={"plugin_id": manifest.plugin_id, "timeout_ms": limits.timeout_ms},
            )

        body = self._read_frame(store, memory, result_ptr, limits.max_output_bytes)
        _best_effort_free(store, free, result_ptr, len(body) + 4)

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise PluginInvalidOutputError(
                "plugin response is not valid JSON",
                details={"plugin_id": manifest.plugin_id},
            ) from exc
        if not isinstance(data, dict):
            raise PluginInvalidOutputError(
                f"plugin response must be a JSON object, got {type(data).__name__}",
                details={"plugin_id": manifest.plugin_id},
            )
        if manifest.output_schema:
            try:
                Draft202012Validator(dict(manifest.output_schema)).validate(data)
            except JsonSchemaValidationError as exc:
                raise PluginInvalidOutputError(
                    f"response does not match the declared schema: {exc.message}",
                    details={"plugin_id": manifest.plugin_id},
                ) from exc

        fuel_used = limits.fuel - int(store.get_fuel())
        return data, fuel_used

    @staticmethod
    def _read_frame(
        store: wasmtime.Store,
        memory: wasmtime.Memory,
        ptr: int,
        max_bytes: int,
    ) -> bytes:
        if ptr <= 0:
            raise PluginInvalidOutputError(f"plugin returned an invalid result pointer ({ptr})")
        header = bytes(memory.read(store, ptr, ptr + 4))
        length = frame_length(header)
        if length > max_bytes:
            raise PluginInvalidOutputError(
                f"plugin response of {length} bytes exceeds the {max_bytes} byte limit",
                details={"bytes": length, "limit": max_bytes},
            )
        body = bytes(memory.read(store, ptr + 4, ptr + 4 + length))
        return decode_frame(header + body, max_bytes=max_bytes)

    @staticmethod
    def _translate_trap(plugin_id: str, exc: wasmtime.Trap, *, timed_out: bool) -> PluginError:
        code = exc.trap_code
        name = code.name if code else "unknown"
        if code is wasmtime.TrapCode.OUT_OF_FUEL:
            return PluginResourceError(
                "plugin exhausted its fuel budget",
                details={"plugin_id": plugin_id, "trap": name},
            )
        if code is wasmtime.TrapCode.INTERRUPT:
            if timed_out:
                return PluginTimeoutError(
                    "plugin exceeded its wall-clock budget",
                    details={"plugin_id": plugin_id, "trap": name},
                )
            return PluginTimeoutError(
                "plugin was interrupted before completing",
                details={"plugin_id": plugin_id, "trap": name},
            )
        return PluginTrapError(
            f"plugin trapped: {name}",
            details={"plugin_id": plugin_id, "trap": name},
        )

    def _failed(
        self,
        plugin_id: str,
        exc: PluginError,
        started: float,
        *,
        bridge: _HostBridge | None = None,
        sink: _BoundedSink | None = None,
    ) -> InvocationResult:
        self._logger.warning("plugin %s failed: %s", plugin_id, exc)
        return InvocationResult(
            plugin_id=plugin_id,
            ok=False,
            data=None,
            error=exc.to_info(),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            fuel_used=None,
            host_calls=bridge.outcomes if bridge else (),
            stdout=sink.text() if sink else "",
            # A failed invocation still reports what it spent. The budget is what
            # makes "why did it stop" answerable after the fact.
            cost_used=bridge.scope.cost_spent if bridge else 0,
            cost_budget=bridge.scope.max_cost if bridge else 0,
        )

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        """Forget loaded modules. Stores are per-invocation, so nothing is held."""
        self._plugins.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "HOST_CALL_TIMEOUT_S",
    "STDIO_CAPTURE_BYTES",
    "InvocationResult",
    "WasmPlugin",
    "WasmPluginHost",
]
