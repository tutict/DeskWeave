"""The install lifecycle: discover, install, inspect, approve, activate, revoke.

Phase 2.5 answered "are these the bytes that were installed". This module answers
the three questions that have nothing to do with bytes:

* **Who decided this plugin may ask for these capabilities?** — ``approve``. An
  approval is a person's decision, bound to one build, and it is the only thing
  that creates permission grants *or takes them away*: the granted set is
  reconciled with the decision on every call, because an approval that only ever
  adds is how a capability the user narrowed away keeps running.
* **May *this* build run?** — ``activate``. The guard compares the live module
  against the approval and refuses when the version, the content digest, the ABI
  version or the *requested capability set* has moved. A new build never inherits
  an old approval, and a manifest that grew a capability is not "the same plugin".
* **What is actually true right now?** — ``inspect``. Requested, approved and
  granted are three separate sets, printed side by side, because collapsing them
  is how "the plugin said it wanted X" becomes "X was allowed".

The three sets, deliberately distinct:

=================  ============================================  ==========================
Set                Written by                                    Means
=================  ============================================  ==========================
``requested``      the plugin author (manifest)                  a claim, never a grant
``approved``       a person (``approve``)                        what that person agreed to
``granted``        ``PermissionManager`` rows                    what dispatch will honour
=================  ============================================  ==========================

Nothing in this module turns one into the next by implication. Installing does
not approve; a matching hash does not approve; a verified ABI does not approve.

What this is **not**: publisher identity. An approval is a local record of a local
decision, in the same database (or the same directory) as the grants, under the
same OS user. Someone who can rewrite that store can rewrite the decision. See
``docs/architecture/plugin-lifecycle.md`` §7.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import os
import tempfile
import threading
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from deskweave_contracts import PermissionRequest

from .errors import (
    PluginError,
    PluginLifecycleError,
    PluginNotApprovedError,
    PluginNotFoundError,
    plugin_error,
)
from .execution import RevocationBarrier
from .host import HostCapabilityRegistry, principal_for
from .integrity import (
    INSTALL_RECORD_NAME,
    InstallRecord,
    install,
    normalise_module_path,
    sha256_file,
)
from .locking import StoreLock
from .locking import acquire as acquire_store_lock
from .locking import release as release_store_lock
from .manifest import WasmPluginManifest
from .runtime import ActivationGuard, WasmPlugin, WasmPluginHost

APPROVAL_RECORD_VERSION = 1

#: One revocation barrier per approval-store path.
#:
#: Two lifecycles opening the same store must block the same plugins, or a
#: revocation committed through one leaves a call queued in the other's executor
#: admissible. The store path is the only identifier they share, so it is the
#: key. ``WeakValueDictionary`` so a barrier dies with the last lifecycle using
#: it rather than accumulating one entry per path ever opened.
_BARRIERS: weakref.WeakValueDictionary[str, RevocationBarrier] = weakref.WeakValueDictionary()
#: Barriers for stores with no path, keyed by the store *object*: sharing the
#: object is the only way to share records, so it also shares the block.
_STORE_BARRIERS: weakref.WeakKeyDictionary[object, RevocationBarrier] = weakref.WeakKeyDictionary()
#: Guards the get-or-create in :func:`_barrier_for`. Construction may happen on
#: more than one thread, and two barriers for one store is the bug, not the race.
_BARRIER_LOCK = threading.Lock()


def _canonical_path(path: str | Path) -> str:
    """One spelling for "this file" on this platform.

    ``resolve()`` collapses ``.``, ``..`` and symlinks; ``normcase`` folds case
    and separators, because Windows paths are case-insensitive and two spellings
    genuinely name one file. Non-strict resolution on purpose: a manifest has to
    be describable before it exists.

    This exists because Phase 2.6.2 compared the *stored string*, so installing
    the same bytes through a relative path looked like a different installation
    and silently discarded a live approval.
    """
    return os.path.normcase(str(Path(path).resolve()))


def _grant_expired(row: dict[str, Any]) -> bool:
    """Would ``check`` refuse this grant row for being past its expiry?

    A local copy of the rule in ``deskweave_runtime.permissions`` on purpose:
    this package must not import the runtime (the dependency points the other
    way). It is four lines, and it exists only for authorities that expose the
    raw ``list_grants`` — one that offers ``list_effective_grants`` is asked
    instead, so the two can only disagree if both are wrong in the same way.

    Missing expiry means no expiry; an unparseable value counts as expired,
    because a typo must not become a permanent grant.
    """
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    try:
        when = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when < datetime.now(UTC)


def _barrier_for(store: ApprovalStore) -> RevocationBarrier:
    """The barrier shared by every lifecycle acting on ``store``.

    A store has one of two identities, and both have to be honoured:

    * a **file** store is "that path", so two `JsonApprovalStore` objects opened
      on the same file share the block;
    * an **in-memory** store has no path, but its *instance* can still be handed
      to two lifecycles — they then share one set of records. Keying by the object
      means sharing the store also shares the block. (2.6.3 first shipped "there
      is nothing another instance could share" here; that was wrong, and a probe
      showed two lifecycles over one `MemoryApprovalStore` with two barriers.)

    The get-or-create runs under a lock because construction can happen on
    different threads: two concurrent `PluginLifecycle()` calls on one store must
    not each build a barrier, which would silently restore the split this exists
    to prevent.

    A path that cannot be resolved is refused rather than papered over: if this
    lifecycle cannot say *which* store it is on, it cannot know whether another
    host is blocking the same plugin, and running with a private barrier would be
    a guarantee that quietly is not one. The same rule applies to a pathless store
    that cannot be used as a weak key: that is an identity that cannot be shared,
    so it is refused instead of degrading to a private barrier (Phase 2.6.4 F5 —
    the failure direction has to match the file-backed branch).
    """
    path = getattr(store, "path", None)
    if path is None:
        with _BARRIER_LOCK:
            try:
                existing = _STORE_BARRIERS.get(store)
                if existing is not None:
                    return existing
                barrier = RevocationBarrier()
                _STORE_BARRIERS[store] = barrier
                return barrier
            except TypeError as exc:  # not weak-referenceable, or not hashable
                raise plugin_error(
                    PluginLifecycleError,
                    "this approval store cannot be used as a shared identity: it is neither "
                    "weak-referenceable nor hashable, so two lifecycles over it would each "
                    "get their own revocation barrier",
                    store=type(store).__name__,
                ) from exc
    try:
        key = _canonical_path(path)
    except (OSError, ValueError) as exc:
        raise plugin_error(
            PluginLifecycleError,
            "the approval store's path cannot be resolved, so this lifecycle cannot "
            "share its revocation barrier with any other host",
            path=str(path),
        ) from exc
    with _BARRIER_LOCK:
        existing = _BARRIERS.get(key)
        if existing is not None:
            return existing
        barrier = RevocationBarrier()
        _BARRIERS[key] = barrier
        return barrier


class InstallState(StrEnum):
    """Where a plugin is in its life. Every value is reachable, none is a guess."""

    #: A manifest was found on disk. Nothing has been verified or pinned.
    DISCOVERED = "discovered"
    #: Pinned: the install record exists and the bytes matched it when written.
    INSTALLED = "installed"
    #: A person approved a capability set for this exact build.
    APPROVED = "approved"
    #: Loaded and running under that approval.
    ACTIVE = "active"
    #: The approval was withdrawn and the grants are gone. The module may still
    #: be on disk and may still be loaded in memory; it can no longer ask for
    #: anything.
    REVOKED = "revoked"
    #: Pin, approval and grants removed. The ``.wasm`` is left alone.
    UNINSTALLED = "uninstalled"


class LifecycleEvent(StrEnum):
    """The last thing that happened, recorded so ``inspect`` can say what."""

    DISCOVERED = "discovered"
    INSTALLED = "installed"
    REINSTALLED = "reinstalled"
    APPROVED = "approved"
    ACTIVATED = "activated"
    ACTIVATION_REFUSED = "activation_refused"
    REVOKED = "revoked"
    UNINSTALLED = "uninstalled"


#: Legal transitions. Anything not listed raises ``PluginLifecycleError``;
#: the table is the specification, and the tests walk every edge.
_TRANSITIONS: dict[InstallState, frozenset[InstallState]] = {
    InstallState.DISCOVERED: frozenset({InstallState.INSTALLED, InstallState.UNINSTALLED}),
    # Re-pinning is allowed from any installed-or-later state, including while
    # active: an upgrade is a normal operation, and refusing it would push people
    # to uninstall first (which deletes the very record they want to compare
    # against). What the transition costs is the approval — ``install`` writes
    # ``approval=None`` for a *different* build, so an upgrade cannot inherit the
    # old decision. Installing the bytes that are already pinned writes nothing at
    # all, and therefore keeps the approval: see ``_installation_mismatch``.
    InstallState.INSTALLED: frozenset(
        {InstallState.APPROVED, InstallState.UNINSTALLED, InstallState.INSTALLED}
    ),
    InstallState.APPROVED: frozenset(
        {
            InstallState.ACTIVE,
            InstallState.REVOKED,
            InstallState.UNINSTALLED,
            InstallState.INSTALLED,
            # An approval may be *revised* in place while it is not yet in use:
            # "approve {A,B}" followed by "actually, only {A}" is one decision
            # made twice, not an illegal edge. What it must not do is change
            # under a running plugin, which is why ACTIVE is not in this set.
            InstallState.APPROVED,
        }
    ),
    InstallState.ACTIVE: frozenset(
        {
            InstallState.REVOKED,
            InstallState.UNINSTALLED,
            InstallState.ACTIVE,
            InstallState.INSTALLED,
        }
    ),
    # ``revoked → revoked`` is allowed for the same reason as the uninstalled
    # self-loop below: the decision is written *before* the grants are withdrawn,
    # so a failure at the grants step leaves a persisted revocation and a residual
    # grant. Re-running the verb finishes that cleanup. It is a withdrawal, not a
    # decision: it writes no approval, creates no grant, and does not lift the
    # block — otherwise "I tried again" would restore the capability the user took
    # away. Without this edge the only remaining verbs are ``install`` (which does
    # not clean grants) and ``uninstall`` (which deletes the pin), so a revocation
    # that failed at its last step would be unrecoverable by the verb that failed.
    InstallState.REVOKED: frozenset(
        {InstallState.APPROVED, InstallState.UNINSTALLED, InstallState.REVOKED}
    ),
    # ``uninstalled → uninstalled`` is allowed on purpose. The record is a
    # tombstone, and a first attempt can fail after the tombstone was written
    # (grants store down, pin kept because it was replaced). Without this edge the
    # only way to finish the cleanup is ``install`` followed by ``uninstall``,
    # which needs the module to still be on disk — so a failed uninstall would be
    # unrecoverable by the verb that failed. Re-running it is a cleanup, not a new
    # decision: the record's approval and capability list are already empty.
    InstallState.UNINSTALLED: frozenset(
        {InstallState.INSTALLED, InstallState.DISCOVERED, InstallState.UNINSTALLED}
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_waiter(waiter: asyncio.Future[None]) -> None:
    """Complete one drain waiter, on the loop that owns it."""
    if not waiter.done():
        waiter.set_result(None)


def _wake_waiters(waiters: tuple[asyncio.Future[None], ...]) -> None:
    """Wake drain waiters from whichever thread finished the last operation.

    Each waiter belongs to the loop that called ``aclose()``, which need not be
    the loop the operation was running on. A future may only be completed from its
    own loop, so the wake is *scheduled* there with ``call_soon_threadsafe`` —
    direct ``set_result`` from a foreign thread does not wake a selector that is
    blocked in ``select()``, and the waiter never runs again (Phase 2.6.9a).

    A loop that is already closed cannot run the callback at all; the waiter is
    then cancelled rather than left pending, so the coroutine waiting on it does
    not stay parked in a loop that will never advance.
    """
    for waiter in waiters:
        if waiter.done():  # pragma: no cover - a waiter that gave up on its own
            continue
        try:
            waiter.get_loop().call_soon_threadsafe(_resolve_waiter, waiter)
        except RuntimeError:  # pragma: no cover - the waiter's loop is gone
            waiter.cancel()


@dataclass(frozen=True)
class PluginApproval:
    """A person's decision, bound to one build. Not a signature, not a capability."""

    plugin_id: str
    version: str
    interface_version: int
    wasm_sha256: str
    #: What the user said yes to. May be a strict subset of ``requested``.
    capabilities: frozenset[str]
    #: The capability set the manifest asked for *at approval time*. Kept so that
    #: "the plugin grew a capability" is detectable as a change, not inferred
    #: from the current manifest — which the plugin author controls.
    requested: frozenset[str]
    approved_by: str
    approved_at: str
    approval_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "interface_version": self.interface_version,
            "wasm_sha256": self.wasm_sha256,
            "capabilities": sorted(self.capabilities),
            "requested": sorted(self.requested),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approval_id": self.approval_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PluginApproval:
        try:
            return cls(
                plugin_id=str(raw["plugin_id"]),
                version=str(raw["version"]),
                interface_version=int(raw["interface_version"]),
                wasm_sha256=str(raw["wasm_sha256"]),
                capabilities=frozenset(str(c) for c in raw["capabilities"]),
                requested=frozenset(str(c) for c in raw.get("requested", raw["capabilities"])),
                approved_by=str(raw.get("approved_by", "unknown")),
                approved_at=str(raw.get("approved_at", "")),
                approval_id=str(raw.get("approval_id", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise plugin_error(
                PluginLifecycleError,
                "the approval record is malformed; refusing to treat it as absent",
                problem=str(exc),
            ) from exc


@dataclass(frozen=True)
class ApprovalCheck:
    """The result of comparing a live build against an approval."""

    ok: bool
    reason: str
    #: Capabilities the manifest now asks for that it did not ask for when it was
    #: approved. Non-empty means *escalation*: refuse and require re-approval.
    added: tuple[str, ...] = ()
    #: Capabilities that were requested at approval time and no longer are.
    #: Harmless (narrowing), but recorded rather than silently forgotten.
    dropped: tuple[str, ...] = ()
    #: Approved but not requested — the user allowed something the plugin never
    #: asked for. Reported so the approval surface can be tidied; not a refusal.
    unused: tuple[str, ...] = ()


def check_approval(
    approval: PluginApproval,
    manifest: WasmPluginManifest,
    digest: str,
) -> ApprovalCheck:
    """Does ``approval`` still cover ``manifest``/``digest``?

    The single source of truth for this comparison: the load-time guard and
    ``activate``'s audit record both call it, so they cannot disagree.
    """
    if approval.plugin_id != manifest.plugin_id:
        return ApprovalCheck(False, "the approval names a different plugin id")

    requested = manifest.declared_capabilities
    added = tuple(sorted(requested - approval.requested))
    dropped = tuple(sorted(approval.requested - requested))
    unused = tuple(sorted(approval.capabilities - requested))

    if approval.wasm_sha256 != digest:
        # Checked before version on purpose: the same version with different
        # bytes is the more alarming of the two, and it is the one a "rebuild in
        # place" produces.
        return ApprovalCheck(
            False, "the module content changed since it was approved", added, dropped, unused
        )
    if approval.version != manifest.version:
        return ApprovalCheck(
            False, "the plugin version changed since it was approved", added, dropped, unused
        )
    if approval.interface_version != manifest.interface_version:
        return ApprovalCheck(
            False, "the plugin's ABI version changed since it was approved", added, dropped, unused
        )
    if added:
        return ApprovalCheck(
            False,
            "the manifest now requests capabilities that were not approved: " + ", ".join(added),
            added,
            dropped,
            unused,
        )
    return ApprovalCheck(True, "approved", added, dropped, unused)


@dataclass(frozen=True)
class LifecycleRecord:
    """Everything known about one plugin, in one place."""

    plugin_id: str
    state: InstallState
    version: str = ""
    interface_version: int = 0
    wasm_sha256: str = ""
    module_name: str = ""
    #: Where the manifest lives, so ``uninstall`` can find the pin it wrote.
    #: Stored as given; never printed in an error message.
    manifest_path: str = ""
    capabilities: tuple[str, ...] = ()
    approval: PluginApproval | None = None
    #: The approval a reinstall replaced. Kept so that "this build is not the one
    #: you approved" can name *why* (version, content, capabilities added) instead
    #: of degrading to "no approval on file" the moment the pin is refreshed.
    previous_approval: PluginApproval | None = None
    last_event: LifecycleEvent | None = None
    last_detail: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_version": APPROVAL_RECORD_VERSION,
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "version": self.version,
            "interface_version": self.interface_version,
            "wasm_sha256": self.wasm_sha256,
            "module_name": self.module_name,
            "manifest_path": self.manifest_path,
            "capabilities": list(self.capabilities),
            "approval": None if self.approval is None else self.approval.as_dict(),
            "previous_approval": (
                None if self.previous_approval is None else self.previous_approval.as_dict()
            ),
            "last_event": None if self.last_event is None else self.last_event.value,
            "last_detail": self.last_detail,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LifecycleRecord:
        try:
            approval_raw = raw.get("approval")
            previous_raw = raw.get("previous_approval")
            return cls(
                plugin_id=str(raw["plugin_id"]),
                state=InstallState(str(raw["state"])),
                version=str(raw.get("version", "")),
                interface_version=int(raw.get("interface_version", 0)),
                wasm_sha256=str(raw.get("wasm_sha256", "")),
                module_name=str(raw.get("module_name", "")),
                manifest_path=str(raw.get("manifest_path", "")),
                capabilities=tuple(str(c) for c in raw.get("capabilities", ())),
                approval=None if approval_raw is None else PluginApproval.from_dict(approval_raw),
                previous_approval=(
                    None if previous_raw is None else PluginApproval.from_dict(previous_raw)
                ),
                last_event=(
                    None
                    if raw.get("last_event") is None
                    else LifecycleEvent(str(raw["last_event"]))
                ),
                last_detail=dict(raw.get("last_detail") or {}),
                updated_at=str(raw.get("updated_at", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise plugin_error(
                PluginLifecycleError,
                "the lifecycle record is malformed; refusing to guess at its state",
                problem=str(exc),
            ) from exc


class ApprovalStore(Protocol):
    """Where approvals live between processes.

    Deliberately a protocol: the desktop runtime will back this with the SQLite
    database it already owns, while tests and the read-only CLI use a file. The
    store is *policy state*, not a trust root — see the module docstring.
    """

    async def load_all(self) -> dict[str, LifecycleRecord]: ...

    async def save(self, record: LifecycleRecord) -> None: ...

    async def delete(self, plugin_id: str) -> None: ...


class MemoryApprovalStore:
    """In-process store. Loses everything on exit, which is honest for tests."""

    def __init__(self) -> None:
        self._records: dict[str, LifecycleRecord] = {}

    async def load_all(self) -> dict[str, LifecycleRecord]:
        return dict(self._records)

    async def save(self, record: LifecycleRecord) -> None:
        self._records[record.plugin_id] = record

    async def delete(self, plugin_id: str) -> None:
        self._records.pop(plugin_id, None)


class JsonApprovalStore:
    """One JSON file, written atomically.

    Not a security boundary. Anyone who can write this file can approve anything,
    exactly as they could write the SQLite database directly. What the atomic
    write buys is that a crash leaves either the old file or the new one, never a
    half-written record that would be parsed as "no approval" (fail-closed but
    silently lossy) or worse, as a truncated approval.

    The path is **fixed at construction** — resolved to an absolute path once —
    because two things key on it and both must agree with the file that is
    actually read and written: the revocation barrier registry and the
    cross-process lock. Keeping the caller's string meant a `chdir` between two
    constructions produced two barriers over one file, and a queued call was
    admitted after a revocation (measured, Phase 2.6.4 F3).
    """

    def __init__(self, path: str | Path) -> None:
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError) as exc:
            raise plugin_error(
                PluginLifecycleError,
                "the approval store's path cannot be resolved, so this store has no "
                "identity to share a barrier or a lock under",
                path=str(path),
            ) from exc
        self._path = resolved
        self._refuse_multiple_links()

    def _refuse_multiple_links(self) -> None:
        """Refuse a store file reachable through more than one name.

        Hard links share content but not path, so two names would get two
        barriers (and, later, two locks) over one file — the exact split the
        barrier exists to prevent. Supporting them needs a file identity
        (inode/file id), which is not portable and changes when the file is
        replaced. Until that is designed, the configuration is refused rather
        than silently degraded.
        """
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            return  # not created yet: one name is all there is
        except OSError as exc:
            raise plugin_error(
                PluginLifecycleError,
                "the approval store could not be inspected, so its identity is unknown",
                path=str(self._path),
            ) from exc
        if stat.st_nlink > 1:
            raise plugin_error(
                PluginLifecycleError,
                "the approval store has more than one hard link; this host identifies a "
                "store by its path, so two names would each get their own revocation "
                "barrier and process lock over one file",
                path=str(self._path),
                links=stat.st_nlink,
            )

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise plugin_error(
                PluginLifecycleError,
                "the approval store is unreadable; refusing to treat it as empty",
                path=self._path.name,
                problem=str(exc),
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), dict):
            raise plugin_error(
                PluginLifecycleError,
                "the approval store has an unexpected shape; refusing to guess",
                path=self._path.name,
            )
        return raw

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def load_all(self) -> dict[str, LifecycleRecord]:
        raw = self._read()
        return {
            plugin_id: LifecycleRecord.from_dict(entry)
            for plugin_id, entry in raw.get("plugins", {}).items()
        }

    async def save(self, record: LifecycleRecord) -> None:
        raw = self._read()
        plugins = dict(raw.get("plugins", {}))
        plugins[record.plugin_id] = record.as_dict()
        self._write({"store_version": APPROVAL_RECORD_VERSION, "plugins": plugins})

    async def delete(self, plugin_id: str) -> None:
        raw = self._read()
        plugins = dict(raw.get("plugins", {}))
        if plugin_id not in plugins:
            return
        plugins.pop(plugin_id)
        self._write({"store_version": APPROVAL_RECORD_VERSION, "plugins": plugins})


class CapabilityAuthority(Protocol):
    """The slice of ``PermissionManager`` this module needs.

    Deliberately narrow: granting, revoking and *listing*, nothing else. The
    listing half is not a convenience — an approval that cannot see which grants
    exist cannot take one away, and an approval that only ever adds is how a
    plugin keeps a capability the user narrowed away from it.
    """

    async def grant(self, request: PermissionRequest, *, granted_by: str = ...) -> str: ...

    async def revoke(
        self, adapter_id: str, capabilities: Iterable[str] | None = None
    ) -> list[str]: ...

    async def list_grants(self) -> list[dict[str, Any]]: ...


class ExpiryAwareAuthority(CapabilityAuthority, Protocol):
    """An authority that can tell "still authorises" from "was once granted".

    ``PermissionManager`` implements this. It is a separate protocol rather than
    a member of :class:`CapabilityAuthority` because an authority that cannot
    answer the expiry question is still usable — this module then applies its own
    predicate to ``list_grants`` — and pretending the method were mandatory would
    reject implementations that do work correctly.
    """

    async def list_effective_grants(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DisclosedPlugin:
    """What ``discover`` can say without loading anything."""

    plugin_id: str
    version: str
    interface_version: int
    manifest_name: str
    module_name: str
    requested: tuple[str, ...]
    declared_sha256: str | None
    pinned: bool


@dataclass(frozen=True)
class RevocationReport:
    """The truth about what revoking did and did not stop.

    Three different things happen to three different kinds of work, and the
    report keeps them apart instead of summing them into one number:

    * calls still **queued** for a slot never started and never will — counted
      by ``queued_stopped``;
    * calls already **admitted** keep running, because this host has no
      preemption — counted by ``running`` (and by ``still_running``, which is
      the same number under its older name);
    * nothing at all is claimed about calls that finished earlier.

    ``stopped`` stays ``False``: some queued work *was* stopped, but the field
    answers a narrower question — "was a running operation interrupted?" — and
    widening its meaning would turn a true statement into a false one.

    **Scope of the counts.** They describe the executor ``revoke()`` was called
    through, because that is the only queue this call drains. A call waiting in a
    *different* host over the same store is not counted here: the shared barrier
    refuses it when its turn comes, so it never runs, but it was never in this
    queue and is not this report's answer.
    ``test_a_queued_call_in_another_host_never_runs_after_a_revocation`` pins both
    halves — that host's call is refused, and this one's ``queued_stopped`` is
    zero.
    """

    plugin_id: str
    revoked_grants: tuple[str, ...]
    was_active: bool
    #: Host operations that had been admitted and had not finished when the
    #: approval was withdrawn. They were **not** stopped.
    still_running: int
    #: Calls that were waiting for a slot and were refused before they started.
    queued_stopped: int = 0
    #: The number of operations that are genuinely in flight. Same value as
    #: ``still_running``; named for what it is.
    running: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class Inspection:
    """The read-only answer ``inspect`` returns. No side effects, no host calls."""

    plugin_id: str
    state: InstallState
    version: str
    interface_version: int
    module_name: str
    #: The hash the manifest on disk claims, if it claims one.
    declared_sha256: str | None
    #: The hash of the bytes on disk, computed now. ``None`` if the file is gone.
    actual_sha256: str | None
    #: The hash the approval was bound to — the build a person actually saw.
    approved_sha256: str | None
    #: ``pinned-and-matching`` / ``pinned-but-modified`` / ``record-missing`` /
    #: ``module-missing`` / ``not-checked``.
    pin_state: str
    requested: tuple[str, ...]
    approved: tuple[str, ...]
    granted: tuple[str, ...]
    #: Would ``activate`` accept this build right now? ``None`` when there is no
    #: approval to compare against.
    drift: ApprovalCheck | None
    #: Is an approval **in force** for this plugin right now? ``drift.ok`` only
    #: says the build still matches the approval that was recorded; it says
    #: nothing about whether that approval is currently in effect. A revoked
    #: plugin keeps its approval on file as history, so ``drift.ok=True`` next to
    #: ``state=revoked`` is the normal case and must never be read as
    #: "authorised". This field is the one that means authorised.
    approval_in_force: bool
    last_event: LifecycleEvent | None
    last_detail: dict[str, Any]
    notes: tuple[str, ...] = ()


def _read_manifest(path: Path) -> WasmPluginManifest | None:
    try:
        return WasmPluginManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def discover(directory: str | Path) -> list[DisclosedPlugin]:
    """Read every manifest in ``directory`` (non-recursive). Never loads a module."""
    root = Path(directory)
    if not root.is_dir():
        raise PluginNotFoundError("plugin directory not found", details={"path": root.name})
    found: list[DisclosedPlugin] = []
    for manifest_path in sorted(root.glob("*.json")):
        if manifest_path.name == INSTALL_RECORD_NAME:
            continue
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            continue
        module_name = normalise_module_path(
            manifest.wasm_path or f"{manifest_path.stem}.wasm", plugin_id=manifest.plugin_id
        )
        found.append(
            DisclosedPlugin(
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                interface_version=manifest.interface_version,
                manifest_name=manifest_path.name,
                module_name=module_name,
                requested=tuple(sorted(manifest.declared_capabilities)),
                declared_sha256=manifest.wasm_sha256,
                pinned=(manifest_path.parent / INSTALL_RECORD_NAME).is_file(),
            )
        )
    return found


#: How many protected operations of one lifecycle are running *in this context*.
#: Only used to refuse ``aclose()`` from inside an operation it would then wait
#: for: a task that waits on itself hangs with no error, which is the one failure
#: mode a shutdown path must not have.
_IN_FLIGHT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "deskweave_lifecycle_in_flight_depth", default=0
)


def _in_flight(method: Any) -> Any:
    """Count one protected write for the whole life of the call.

    The count is what lets ``close()`` keep the store lock until the last
    operation that could still write has returned. It is not a re-entrancy
    guard and does not replace ``_require_open()``: this bookends the *entry
    points*, ``_require_open()`` stays in ``_save`` as the last line of defence
    for anything added later.

    ``finally`` is deliberate — a cancelled or raising operation must still
    decrement, or the lock would never be released (Phase 2.6.7b, A-Test-4).
    """

    @functools.wraps(method)
    async def tracked(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._register()
        try:
            return await method(self, *args, **kwargs)
        finally:
            self._unregister()

    return tracked


class PluginLifecycle:
    """The state machine. One instance per host; it owns that host's guard.

    The host is constructed *here* rather than passed in, so that a lifecycle can
    never exist next to a host whose guard was forgotten. Tests that do not want a
    lifecycle keep constructing ``WasmPluginHost`` directly, unchanged.
    """

    def __init__(
        self,
        *,
        registry: HostCapabilityRegistry,
        permissions: CapabilityAuthority,
        store: ApprovalStore | None = None,
        require_install_record: bool = True,
        logger: Any | None = None,
    ) -> None:
        self._store: ApprovalStore = store if store is not None else MemoryApprovalStore()
        self._permissions = permissions
        self._require_install_record = require_install_record
        self._logger = logger or logging.getLogger("deskweave.wasm.lifecycle")
        self._records: dict[str, LifecycleRecord] = {}
        self._registry = registry
        #: Whether this instance has released the store. Set by :meth:`close`,
        #: checked by every entry point that can write, and never cleared: a
        #: lifecycle that let go of the lock cannot get it back, because the
        #: process that took it in between would then have two writers.
        self._closed = False
        #: Protected operations currently between ``_register`` and
        #: ``_unregister``. The store lock is not released while this is non-zero:
        #: an operation that is parked inside an ``await`` still writes afterwards
        #: (grants, pin), and releasing first would let another process govern the
        #: store underneath it (Phase 2.6.7a, measured with a real second process).
        self._inflight = 0
        #: Guards the pair (``_closed``, ``_inflight``). A ``threading`` lock on
        #: purpose: the critical section has no ``await``, so "check open and
        #: register" cannot interleave with "close and decide whether to release",
        #: and ``close()`` stays callable from a sync context. An ``asyncio.Lock``
        #: would leave exactly the window this closes.
        self._inflight_lock = threading.Lock()
        #: Waiters for "nothing in flight", one future per ``aclose()`` call, each
        #: created on *its caller's* loop. Deliberately not an ``asyncio.Event``:
        #: the operation that clears the count may be running on a different loop
        #: and thread, and ``Event.set()`` from there does not wake a waiter parked
        #: on another loop — it appends to that loop's ready queue without waking
        #: its selector, so the waiter parks forever. Measured in Phase 2.6.9a at
        #: 8 s, with the store already released and the host never closed. Each
        #: waiter's own loop is told about the wake with ``call_soon_threadsafe``,
        #: which is the only thread-safe way to reach it.
        self._drain_waiters: set[asyncio.Future[None]] = set()
        # One writer at a time. Every transition is read-modify-write across two
        # stores that cannot share a transaction (grants in SQLite, decisions in
        # the approval store), so an interleaved pair of transitions could save a
        # record built from state that no longer exists. Serialising them here is
        # the only place that can be done without a distributed lock.
        self._lock = asyncio.Lock()
        # Plugins whose withdrawal is in progress. Read by the dispatch guard,
        # which must refuse *during* the revocation and not only after it, so a
        # call that arrives mid-flight cannot slip through the window where the
        # grants are gone but the record has not been written yet.
        self._revoking: set[str] = set()
        # Two lifecycles over one store are a supported configuration (different
        # surfaces, one decision record). They cannot share the transition lock —
        # it is an ``asyncio.Lock`` and two of them may live on different loops —
        # so the one thing that *must* be shared is the set of plugins whose
        # admission is blocked, keyed by the store path they have in common.
        self._barrier = _barrier_for(self._store)
        # One process per store. The barrier above is process memory, so a second
        # process has its own and a revocation cannot cross to it; that is the
        # constraint this lock turns from documentation into behaviour (F, Batch
        # B). It is taken before the host is built, so a refusal leaves nothing
        # half-constructed.
        self._store_lock: StoreLock | None = None
        self._store_lock_finalizer: weakref.finalize | None = None
        store_path = getattr(self._store, "path", None)
        if store_path is not None:
            self._store_lock = acquire_store_lock(Path(store_path))
            # A backstop, not the supported release path: `close()` is, and
            # `__del__` is deliberately not used. It is weaker than it looks —
            # the registry holds a bound method, so this object is usually not
            # collectable at all — and Phase 2.6.7b leaves that as a stated
            # limitation rather than pretending the finalizer runs.
            self._store_lock_finalizer = weakref.finalize(
                self, release_store_lock, self._store_lock
            )
        try:
            self._host = WasmPluginHost(
                registry=registry,
                logger=logger,
                require_install_record=require_install_record,
                activation_guard=self._guard,
                revocation_barrier=self._barrier,
            )
        except BaseException:
            # The lock this constructor took is released before the failure
            # leaves: otherwise a failed construction would keep the store
            # governed by an object nobody has a reference to.
            self.close()
            raise
        # Installed *last*, and only once everything above succeeded. Until this
        # line the registry cannot reach this instance, so a construction that
        # raises leaves the caller's registry exactly as it was — a guard bound
        # to a half-built lifecycle is a guard that raises ``AttributeError``
        # from inside dispatch (Phase 2.6.5 audit, probe B4).
        registry.dispatch_guard = self._dispatch_guard

    # -- wiring ----------------------------------------------------------------

    @property
    def host(self) -> WasmPluginHost:
        """The guarded host. Loading through it is always approval-checked."""
        return self._host

    @property
    def closed(self) -> bool:
        """Whether this instance has released the store (see :meth:`close`)."""
        return self._closed

    @property
    def inflight(self) -> int:
        """How many protected operations are still running. For diagnostics."""
        with self._inflight_lock:
            return self._inflight

    def close(self) -> None:
        """Stop accepting work; release the store once nothing is in flight.

        Call it when the lifecycle is done. In-process holders share one OS lock,
        so closing one lifecycle does not unlock the store for another that is
        still using it — the lock drops when the last one closes. The lock file
        itself is left in place on purpose (see :mod:`.locking`).

        Closing is **not only** a release: the instance stops being an authority
        at the same moment. ``_closed`` is set under the same lock that counts
        in-flight operations, so a call can be either *refused* or *counted*,
        never both — that is the whole point of the pair. If something is still
        running, the lock is **not** released here: the operation that is parked
        inside an ``await`` will write afterwards (grants, pin), and releasing
        first would let another process govern the store while this one still
        changes it. The last operation out releases the lock instead.

        This method therefore makes a weaker promise than :meth:`aclose`: it
        stops new work and eventually releases the store, but it cannot wait —
        it is synchronous, and blocking the event loop is not an option. The host
        this lifecycle owns is **not** closed here either; call :meth:`aclose`
        for that.
        """
        with self._inflight_lock:
            self._closed = True
            idle = self._inflight == 0
        if idle:
            self._release_store_lock()

    async def aclose(self) -> int:
        """Close, wait for in-flight work, then release the host. Returns leftovers.

        The count is what ``WasmPluginHost.aclose()`` reports: host operations
        that had not finished and cannot be interrupted. Zero is the normal
        answer, and the only answer a caller can rely on.

        Refuses to run from *inside* one of this lifecycle's own operations: it
        would be waiting for the caller to finish. A deadlock with no error is
        the one shutdown failure that cannot be diagnosed from a traceback, so it
        is turned into an exception instead.

        Safe to await from a **different** event loop than the one the in-flight
        operations run on: the wait is woken on the caller's own loop, so a
        shutdown driven from another thread returns instead of parking forever
        (Phase 2.6.9b, R1).

        A host that refuses to shut down is reported as ``plugin_lifecycle`` with
        ``host_closed=False`` and the original exception chained. By then the
        lifecycle is closed and the store lock is already released, so the failure
        says nothing about the store — and calling ``aclose()`` again is both safe
        and the documented way to retry (Phase 2.6.9b, R2).
        """
        if _IN_FLIGHT_DEPTH.get():
            raise plugin_error(
                PluginLifecycleError,
                "aclose() was called from inside an operation of this lifecycle; it "
                "would wait for the caller to finish, so it is refused instead of "
                "deadlocking — close the lifecycle from outside the call",
                lifecycle="closing_self",
            )
        self.close()
        await self._wait_for_drain()
        try:
            return await self._host.aclose()
        except PluginError:
            raise
        except Exception as exc:
            # ``close()`` has already run, so the store lock is released and the
            # instance will not accept work again — but the host is *not* closed,
            # and those two facts must not be confused. Reported with the same
            # shape as every other partial lifecycle outcome
            # (``revocation_persisted``, ``grants_withdrawn``,
            # ``uninstall_persisted``), and retryable: running ``aclose()`` again
            # calls host shutdown again, which is why nothing here records a
            # "already attempted" flag.
            raise plugin_error(
                PluginLifecycleError,
                "the lifecycle is closed and the store is released, but the host could "
                "not be shut down; calling aclose() again retries it",
                host_closed=False,
            ) from exc

    async def _wait_for_drain(self) -> None:
        """Wait until no protected operation is running — from any loop, any thread.

        The count is read and the waiter registered under the same lock that
        ``_unregister`` decrements it under, so an operation that finishes in
        between cannot be missed; once ``close()`` has run no new operation can be
        counted, so the count only falls and a single registration is enough.

        The waiter is a future on *this* caller's loop. Waking it is
        ``_unregister``'s job, and it does so with ``loop.call_soon_threadsafe``
        rather than ``set_result`` directly: the operation that clears the count
        may be on another thread, and a future may only be completed from its own
        loop. That is the difference between a shutdown that returns and one that
        parks forever (Phase 2.6.9a/2.6.9b, R1).
        """
        loop = asyncio.get_running_loop()
        with self._inflight_lock:
            if self._inflight == 0:
                return
            waiter: asyncio.Future[None] = loop.create_future()
            self._drain_waiters.add(waiter)
        try:
            await waiter
        finally:
            with self._inflight_lock:
                self._drain_waiters.discard(waiter)

    def _register(self) -> None:
        """Count this operation, or refuse it because close() has begun.

        The check and the increment happen under the same lock ``close()`` uses,
        so there is no window between "is it open?" and "I am now in flight" for a
        close to slip into (Phase 2.6.7b, invariant A4).
        """
        with self._inflight_lock:
            if self._closed:
                raise plugin_error(
                    PluginLifecycleError,
                    "this lifecycle is closed: it has released the approval store and can no "
                    "longer vouch for or change what the store says; construct a new one",
                    lifecycle="closed",
                )
            self._inflight += 1
        _IN_FLIGHT_DEPTH.set(_IN_FLIGHT_DEPTH.get() + 1)

    def _unregister(self) -> None:
        """Stop counting this operation; release the store if it was the last.

        Runs from ``finally``, so cancellation and exceptions decrement too. The
        release decision is taken inside the lock and performed outside it,
        because :meth:`_release_store_lock` takes the same non-reentrant lock.
        """
        depth = _IN_FLIGHT_DEPTH.get()
        if depth:
            _IN_FLIGHT_DEPTH.set(depth - 1)
        with self._inflight_lock:
            self._inflight -= 1
            drained = self._inflight == 0
            waiters = tuple(self._drain_waiters) if drained else ()
            if drained:
                self._drain_waiters.clear()
            release = drained and self._closed
        if waiters:
            _wake_waiters(waiters)
        if release:
            self._release_store_lock()

    def _release_store_lock(self) -> None:
        """Release the OS lock at most once, whoever gets here first."""
        with self._inflight_lock:
            lock = self._store_lock
            self._store_lock = None
        if lock is None:
            return
        finalizer = self._store_lock_finalizer
        if finalizer is not None:
            finalizer.detach()
            self._store_lock_finalizer = None
        release_store_lock(lock)

    def _require_open(self) -> None:
        """Refuse to act on a closed lifecycle. The one place that says it.

        A closed lifecycle has released the store, so it can neither see another
        process's decision nor be trusted to write one. Every entry point that
        writes — or that authorises an instantiation — calls this first; the
        message names the reason so a caller can tell it apart from "not
        approved", which is a decision about a plugin rather than about this
        object.
        """
        if self._closed:
            raise plugin_error(
                PluginLifecycleError,
                "this lifecycle is closed: it has released the approval store and can no "
                "longer vouch for or change what the store says; construct a new one",
                lifecycle="closed",
            )

    @property
    def registry(self) -> HostCapabilityRegistry:
        return self._registry

    async def _refresh(self) -> None:
        """Re-read the store before acting on it.

        A lifecycle that trusted the snapshot it took at startup would keep
        believing a plugin is approved after another process revoked it, and the
        activation guard reads *this* cache. ``mtime`` is not enough to decide
        whether it is safe to skip the read, and a stored timestamp is written by
        whoever wrote the file — so the store is read, not sampled.
        """
        self._records = await self._store.load_all()

    def _guard(
        self,
        manifest: WasmPluginManifest,
        digest: str,
        records: Mapping[str, LifecycleRecord],
    ) -> None:
        """Refuse to instantiate a build that no current approval covers.

        ``records`` is the snapshot the caller took after refreshing the store —
        never ``self._records``. Reading the host's own cache here was the
        Phase 2.6.4 F2 hole: after another lifecycle revoked, a direct
        ``host.load()`` still saw "approved" and instantiated the build. The
        snapshot is a required argument now, so there is no path that answers
        this question from a cache.
        """
        self._require_open()
        record = records.get(manifest.plugin_id)
        if record is None or (record.approval is None and record.previous_approval is None):
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {manifest.plugin_id!r} has no approval; install and approve it first",
                plugin_id=manifest.plugin_id,
            )
        if record.approval is None:
            # The pin was refreshed and the previous decision was bound to the old
            # build. Say what changed instead of "no approval" — the difference
            # between "you never approved this" and "you approved a different
            # build, and it grew this capability" is the whole point of the check.
            stale = check_approval(record.previous_approval, manifest, digest)  # type: ignore[arg-type]
            reason = (
                stale.reason
                if not stale.ok
                else "the pin was refreshed, which clears the previous approval"
            )
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {manifest.plugin_id!r} must be re-approved after reinstall: {reason}",
                plugin_id=manifest.plugin_id,
                reason=reason,
                added=list(stale.added),
                dropped=list(stale.dropped),
                requires_reapproval=True,
            )
        if record.state not in (InstallState.APPROVED, InstallState.ACTIVE):
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {manifest.plugin_id!r} is {record.state.value}, not approved",
                plugin_id=manifest.plugin_id,
                state=record.state.value,
            )
        verdict = check_approval(record.approval, manifest, digest)
        if not verdict.ok:
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {manifest.plugin_id!r} is not the build that was approved: "
                f"{verdict.reason}",
                plugin_id=manifest.plugin_id,
                reason=verdict.reason,
                added=list(verdict.added),
                dropped=list(verdict.dropped),
            )

    # -- transitions -----------------------------------------------------------

    async def _dispatch_guard(self, plugin_id: str, capability: str) -> None:
        """Refuse a host call that no *approval in force* covers, right now.

        Three questions are asked before a plugin call runs, and they are not
        interchangeable:

        * the manifest declared the capability (the plugin's own claim);
        * the permission layer allows it (a grant or a one-shot — a fact about
          the grants store, which anything that can write that store can forge);
        * a person approved *this build* for *this capability* (this method).

        Only the third one is about the decision. It is asked per call, against a
        freshly read store, because a revocation in another process has to take
        effect on the next call rather than at the next restart.

        The block is lifted here, not only in ``approve``, because another
        lifecycle (or another process) may have written the approval: this host
        would otherwise keep refusing a plugin the record says is approved. That
        lift is **conditional on the revocation generation read before the
        record**: reading "approved" and then lifting would undo a revocation
        that committed in between — measured, not hypothesised: a call in flight
        at this guard executed after a revocation in a second host, and left the
        shared barrier unblocked. When the generation moved, the call is refused
        instead; refusing one call too many is the safe direction.
        """
        # Captured before the read, deliberately: any block that lands after this
        # point bumps the generation and makes the lift below a no-op.
        observed = self._host.executor.revocation_generation(plugin_id)
        if self._closed:
            # A closed lifecycle has released the store, so it cannot see a
            # revocation committed by the process that took it over, and its
            # barrier is no longer the one the new governor uses. Vouching for an
            # approval from here would be the cross-process hole the store lock
            # exists to close, so it refuses instead — a refusal, not a defect.
            raise plugin_error(
                PluginNotApprovedError,
                "this lifecycle is closed and can no longer vouch for an approval; "
                "construct a new lifecycle to dispatch through this store",
                plugin_id=plugin_id,
                state="lifecycle_closed",
            )
        async with self._lock:
            # `_revoking` knows only about withdrawals this instance started; the
            # barrier's mark is shared by every lifecycle over this store. Both
            # are checked, because either one alone leaves a hole: the local set
            # misses another instance's withdrawal, and checking only the barrier
            # would take a second lock before the common case can answer.
            if plugin_id in self._revoking or self._host.executor.is_withdrawing(plugin_id):
                raise plugin_error(
                    PluginNotApprovedError,
                    f"plugin {plugin_id!r} is having its approval withdrawn",
                    plugin_id=plugin_id,
                    state=InstallState.REVOKED.value,
                )
            await self._refresh()
            record = self._records.get(plugin_id)
        if record is None:
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {plugin_id!r} has no approval record",
                plugin_id=plugin_id,
            )
        if record.state not in (InstallState.APPROVED, InstallState.ACTIVE):
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {plugin_id!r} is {record.state.value}, not approved",
                plugin_id=plugin_id,
                state=record.state.value,
            )
        approval = record.approval
        if approval is None:
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {plugin_id!r} has no approval in force",
                plugin_id=plugin_id,
            )
        if (
            approval.wasm_sha256 != record.wasm_sha256
            or approval.version != record.version
            or approval.interface_version != record.interface_version
        ):
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {plugin_id!r} was re-pinned after it was approved",
                plugin_id=plugin_id,
            )
        if capability not in approval.capabilities:
            raise plugin_error(
                PluginNotApprovedError,
                f"capability {capability!r} was not approved for plugin {plugin_id!r}; "
                "a grant alone does not authorise a plugin call",
                plugin_id=plugin_id,
                capability=capability,
                approved=sorted(approval.capabilities),
            )
        # An approval is in force, so the executor's post-revocation block is
        # lifted here rather than only in `approve`. The record is the shared
        # truth (another lifecycle may have written the approval); the block is
        # shared per store, and leaving it set would refuse a plugin that the
        # record says is approved — but only this read's approval justifies it,
        # hence the generation check.
        if not self._host.executor.lift_if_unchanged(plugin_id, observed):
            raise plugin_error(
                PluginNotApprovedError,
                f"plugin {plugin_id!r} was revoked while this call was being authorised",
                plugin_id=plugin_id,
                capability=capability,
            )

    def _check_transition(self, record: LifecycleRecord | None, target: InstallState) -> None:
        current = record.state if record is not None else InstallState.DISCOVERED
        allowed = _TRANSITIONS[current]
        if target not in allowed:
            raise plugin_error(
                PluginLifecycleError,
                f"cannot move plugin {record.plugin_id if record else '?'} from "
                f"{current.value} to {target.value}",
                state=current.value,
                requested=target.value,
                allowed=sorted(state.value for state in allowed),
            )

    async def _save(self, record: LifecycleRecord) -> LifecycleRecord:
        # The last line of defence: every transition writes through here, so a
        # closed lifecycle cannot reach the store even if a future entry point
        # forgets its own check.
        self._require_open()
        stamped = replace(record, updated_at=_now())
        await self._store.save(stamped)
        self._records[stamped.plugin_id] = stamped
        return stamped

    @_in_flight
    async def discover(self, directory: str | Path) -> list[DisclosedPlugin]:
        """List what is on disk and remember it. Never loads a module."""
        self._require_open()
        found = discover(directory)
        async with self._lock:
            await self._refresh()
            for item in found:
                if item.plugin_id in self._records:
                    continue
                await self._save(
                    LifecycleRecord(
                        plugin_id=item.plugin_id,
                        state=InstallState.DISCOVERED,
                        version=item.version,
                        interface_version=item.interface_version,
                        module_name=item.module_name,
                        manifest_path=str(Path(directory) / item.manifest_name),
                        capabilities=item.requested,
                        last_event=LifecycleEvent.DISCOVERED,
                    )
                )
        return found

    @_in_flight
    async def install(
        self, manifest_path: str | Path, *, replace: bool = False
    ) -> tuple[InstallRecord, LifecycleRecord]:
        """Pin the module. Writes the install record, then the lifecycle record.

        Ordering matters: if pinning raises, no lifecycle record is written, so a
        failed install cannot leave a state that claims to be installed. If
        pinning succeeds and the lifecycle write fails, the pin exists but no
        approval does — the safe direction, because activation needs an approval.

        Installing the bytes that are already pinned is a no-op, and it says so:
        nothing is rewritten, so a live approval survives. Clearing the approval
        because a file was "installed" again would revoke a decision nobody
        revisited — and it did, before this check existed.
        """
        self._require_open()
        path = Path(manifest_path)
        async with self._lock:
            await self._refresh()
            outcome, installed = install(path, replace=replace)
            manifest = WasmPluginManifest.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            existing = self._records.get(installed.plugin_id)
            # ``None`` means the record already describes exactly this install,
            # and then nothing is written: no record, no grant, no byte of the
            # store changes. Anything else is the reason it does not, kept for
            # the audit so "my approval vanished" has an answer.
            mismatch = (
                _installation_mismatch(existing, installed, manifest, path)
                if outcome == "unchanged" and existing is not None
                else f"the pin reported {outcome!r}"
            )
            if mismatch is None:
                return installed, existing
            self._check_transition(existing, InstallState.INSTALLED)
            event = (
                LifecycleEvent.REINSTALLED
                if existing is not None and existing.state is not InstallState.DISCOVERED
                else LifecycleEvent.INSTALLED
            )
            saved = await self._save(
                LifecycleRecord(
                    plugin_id=installed.plugin_id,
                    state=InstallState.INSTALLED,
                    # Written from the *manifest on disk*, not from the pin. Every
                    # field agrees with the pin except ``interface_version``: the
                    # pin's ``unchanged`` test does not look at it, so a build that
                    # kept its bytes and version while changing its ABI would leave
                    # the pin describing the old one. The record is what the guard
                    # compares an approval against, so it has to describe the file.
                    version=manifest.version,
                    interface_version=manifest.interface_version,
                    wasm_sha256=installed.wasm_sha256,
                    module_name=_base_name(installed.wasm_path),
                    # Absolute, so a later call from another working directory
                    # compares equal instead of looking like a new installation.
                    manifest_path=str(Path(path).resolve()),
                    capabilities=tuple(sorted(manifest.declared_capabilities)),
                    approval=None,
                    previous_approval=existing.approval if existing is not None else None,
                    last_event=event,
                    last_detail={
                        "replace": replace,
                        "outcome": outcome,
                        "record_mismatch": mismatch,
                    },
                )
            )
            return installed, saved

    @_in_flight
    async def approve(
        self,
        plugin_id: str,
        capabilities: Iterable[str],
        *,
        approved_by: str = "user",
        approval_id: str = "",
    ) -> PluginApproval:
        """Record a person's decision and make the grants match it exactly.

        Two things this refuses, both deliberately:

        * approving a capability the plugin never requested (``not_requested``) —
          an approval may narrow what a plugin asks for, never invent;
        * approving a capability this host does not offer (``unknown``) — that is
          a typo or a stale catalogue, and either way it must not become a grant.

        A third refusal belongs to this method's real job: when the authority
        cannot enumerate grants, it cannot take one away, so a narrowing approval
        would leave the extra capability granted — and the approval would be a
        lie. That case is refused rather than approximated.

        Ordering, and why it is this way round:

        1. **subtract** the grants the new decision does not cover. If this fails
           the record is not touched, so there is never a stored approval that
           claims a narrowing whose revoke did not happen.
        2. **write** the decision.
        3. **add** the grants the decision needs.

        The two stores cannot share a transaction, so the order is chosen so that
        every partial outcome leaves the plugin able to do *less* than the record
        says, never more: an unwritten record with fewer grants is a nuisance, a
        written record with extra grants in force is a security bug. Step 3 runs
        last for exactly that reason.

        An **empty** capability set is not refused here — the record of "yes, but
        to nothing" is a legitimate answer, and turning it into an error would be
        a new public behaviour this method never had. It does not, however, lift
        the post-revocation block: a decision that authorises no capability
        cannot restore admissibility that a revocation took away (Phase 2.6.8b,
        C3). The plugin then stays refused, now with "not approved" as the reason
        instead of "revoked".
        """
        self._require_open()
        wanted = frozenset(str(c) for c in capabilities)
        async with self._lock:
            await self._refresh()
            record = self._records.get(plugin_id)
            if record is None:
                raise plugin_error(
                    PluginNotFoundError,
                    f"plugin {plugin_id!r} is not known; discover it first",
                    plugin_id=plugin_id,
                )
            self._check_transition(record, InstallState.APPROVED)

            requested = frozenset(record.capabilities)
            not_requested = sorted(wanted - requested)
            if not_requested:
                raise plugin_error(
                    PluginLifecycleError,
                    "cannot approve capabilities the manifest never requested",
                    plugin_id=plugin_id,
                    not_requested=not_requested,
                )
            offered = frozenset(self.registry.capabilities)
            unknown = sorted(wanted - offered)
            if unknown:
                raise plugin_error(
                    PluginLifecycleError,
                    "cannot approve capabilities this host does not offer",
                    plugin_id=plugin_id,
                    unknown=unknown,
                )

            current, enumerable = await self._effective_grants(plugin_id)
            if not enumerable:
                raise plugin_error(
                    PluginLifecycleError,
                    "the capability authority cannot enumerate grants, so an approval "
                    "cannot be reconciled with what dispatch will honour",
                    plugin_id=plugin_id,
                )
            stale = sorted(current - wanted)
            missing = sorted(wanted - current)

            approval = PluginApproval(
                plugin_id=plugin_id,
                version=record.version,
                interface_version=record.interface_version,
                wasm_sha256=record.wasm_sha256,
                capabilities=wanted,
                requested=requested,
                approved_by=approved_by,
                approved_at=_now(),
                approval_id=approval_id,
            )

            if stale:
                # Same capability list, different decision: revoke the difference
                # before anything else, so a failure here cannot be mistaken for
                # "the new approval is in force".
                await self._revoke_grants(plugin_id, stale, action="approval narrowed")

            try:
                await self._save(
                    replace(
                        record,
                        state=InstallState.APPROVED,
                        approval=approval,
                        last_event=LifecycleEvent.APPROVED,
                        last_detail={
                            "approved": sorted(wanted),
                            "approved_by": approved_by,
                            "revoked_stale": stale,
                            "grants_to_create": len(missing),
                        },
                    )
                )
            except Exception as exc:
                # Not transactional, and not described as if it were: the grants
                # in ``stale`` are already gone, the record still holds the old
                # decision, and those two facts are reported separately. The
                # direction is safe — fewer grants than the record says — but the
                # caller must be able to tell this apart from "nothing happened".
                raise plugin_error(
                    PluginLifecycleError,
                    "the approval could not be recorded; the record still holds the "
                    "previous decision",
                    plugin_id=plugin_id,
                    approval_saved=False,
                    revoked_stale=stale,
                    grants_already_revoked=len(stale),
                ) from exc

            granted = 0
            ungranted: list[str] = []
            for capability in missing:
                handler = self.registry.capabilities[capability]
                try:
                    await self._permissions.grant(
                        PermissionRequest(
                            adapter_id=principal_for(plugin_id),
                            capability=capability,
                            scope=handler.scope,
                            risk=handler.risk,
                            requires_confirmation=False,
                            resource=plugin_id,
                            reason=f"approved for plugin {plugin_id} {record.version}",
                        ),
                        granted_by=approved_by,
                    )
                except Exception as exc:  # reported, not swallowed
                    self._logger.warning(
                        "could not create the grant for %s/%s: %s", plugin_id, capability, exc
                    )
                    ungranted.append(capability)
                    continue
                granted += 1

            if ungranted:
                raise plugin_error(
                    PluginLifecycleError,
                    "the approval was recorded, but some grants could not be created; "
                    "the plugin can use fewer capabilities than were approved",
                    plugin_id=plugin_id,
                    approval_saved=True,
                    grants_created=granted,
                    ungranted=ungranted,
                )
            # Only a decision that authorises *something* may lift a block a
            # revocation placed. An empty set creates no grant and covers no
            # capability, so lifting here would make "approve nothing" hand back
            # the admissibility the user took away (Phase 2.6.8b, C3). This is
            # not a new refusal: the empty decision is still recorded and
            # returned, exactly as before — the block simply stays, and the next
            # guarded call is the one that answers, with ``not approved``.
            if wanted:
                self._host.executor.allow_plugin(plugin_id)
            return approval

    async def _grant_rows(self) -> tuple[list[dict[str, Any]], bool]:
        """Rows that would authorise a call *now*, and whether they could be listed.

        Prefers an authority that answers the expiry question itself
        (``list_effective_grants``). Falls back to the raw list with the local
        predicate. Returns ``([], False)`` when the authority cannot enumerate at
        all — ``False`` means "unknown", never "none".
        """
        effective = getattr(self._permissions, "list_effective_grants", None)
        if effective is not None:
            return list(await effective()), True
        raw = getattr(self._permissions, "list_grants", None)
        if raw is None:
            return [], False
        return [row for row in await raw() if not _grant_expired(row)], True

    async def _effective_grants(self, plugin_id: str) -> tuple[frozenset[str], bool]:
        """Capabilities the permission layer would honour for ``plugin_id`` now.

        Second return value is ``False`` when the authority cannot enumerate, in
        which case the first is empty and means "unknown", not "none".

        Expired rows are excluded: they are history, and treating them as current
        authority made ``approve()`` believe a capability was already granted
        while dispatch refused it.
        """
        rows, enumerable = await self._grant_rows()
        if not enumerable:
            return frozenset(), False
        principal = principal_for(plugin_id)
        return (
            frozenset(str(row["capability"]) for row in rows if row.get("adapter_id") == principal),
            True,
        )

    async def _revoke_grants(
        self, plugin_id: str, capabilities: Iterable[str], *, action: str
    ) -> list[str]:
        """Delete named grants, refusing to pretend the decision was applied."""
        names = sorted(capabilities)
        try:
            return await self._permissions.revoke(principal_for(plugin_id), names)
        except Exception as exc:
            raise plugin_error(
                PluginLifecycleError,
                f"{action}: the grants {', '.join(names)} could not be removed, so the "
                "new decision was not saved",
                plugin_id=plugin_id,
                capabilities=names,
                approval_saved=False,
            ) from exc

    @_in_flight
    async def activate(
        self, module_path: str | Path, manifest_path: str | Path | None = None
    ) -> WasmPlugin:
        """Load the plugin, but only if a current approval covers this exact build."""
        self._require_open()
        async with self._lock:
            await self._refresh()
            path = Path(manifest_path) if manifest_path else Path(module_path).with_suffix(".json")
            manifest = WasmPluginManifest.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            # One snapshot for the whole activation: the same object decides which
            # record is consulted, which manifest the guard judges, and which
            # bytes are hashed and instantiated. Nothing below re-reads the file.
            records = dict(self._records)
            record = records.get(manifest.plugin_id)
            if record is None:
                raise plugin_error(
                    PluginNotFoundError,
                    f"plugin {manifest.plugin_id!r} is not known; discover and install it first",
                    plugin_id=manifest.plugin_id,
                )
            if record.state in (InstallState.DISCOVERED, InstallState.UNINSTALLED):
                # There is nothing installed to activate, so this is a sequencing
                # error rather than an authorisation one. Every other state is handed
                # to the guard, which refuses with a reason that names the *decision*
                # that is missing or stale — "not approved" beats "illegal edge" when
                # a person is trying to work out what to do next.
                raise plugin_error(
                    PluginLifecycleError,
                    f"cannot move plugin {manifest.plugin_id!r} from {record.state.value} "
                    "to active",
                    plugin_id=manifest.plugin_id,
                    state=record.state.value,
                    requested=InstallState.ACTIVE.value,
                    allowed=sorted(state.value for state in _TRANSITIONS[InstallState.INSTALLED]),
                )
            try:
                plugin = self._host.load(module_path, path, manifest=manifest, approval=records)
                if plugin.manifest is not manifest:  # pragma: no cover - load re-read the file
                    # The belt to the digest's braces: if a future change makes
                    # `load` parse the manifest again, the record written below
                    # would describe a different object than the one that ran.
                    raise plugin_error(
                        PluginLifecycleError,
                        "the plugin that was loaded is not the manifest this activation "
                        "verified; refusing to record it as active",
                        plugin_id=manifest.plugin_id,
                    )
            except PluginError as exc:
                if isinstance(exc, PluginNotApprovedError):
                    await self._save(
                        replace(
                            record,
                            last_event=LifecycleEvent.ACTIVATION_REFUSED,
                            last_detail={"reason": exc.message, **exc.details},
                        )
                    )
                raise
            except Exception as exc:
                # A guard that raises something other than a PluginError is a
                # defect, and a defect must not read as an ordinary crash: the
                # activation is refused *and* the record says so, otherwise the
                # last event stays whatever it was before and the audit claims
                # nothing happened.
                recorded = True
                try:
                    await self._save(
                        replace(
                            record,
                            last_event=LifecycleEvent.ACTIVATION_REFUSED,
                            last_detail={
                                "reason": (
                                    f"the activation guard raised {type(exc).__name__}: {exc}"
                                ),
                                "error": type(exc).__name__,
                            },
                        )
                    )
                except Exception:  # the store failed too; do not lose the real error
                    recorded = False
                    self._logger.exception("could not record the refused activation")
                raise plugin_error(
                    PluginNotApprovedError,
                    f"plugin {manifest.plugin_id!r} was not activated: the approval check "
                    f"raised {type(exc).__name__}",
                    plugin_id=manifest.plugin_id,
                    error=type(exc).__name__,
                    recorded=recorded,
                ) from exc
            # The table still applies to the record; reaching here means the guard
            # accepted the build, so this can only fail on an internal inconsistency.
            self._check_transition(record, InstallState.ACTIVE)
            verdict = (
                check_approval(record.approval, plugin.manifest, plugin.digest)
                if record.approval is not None
                else None
            )
            await self._save(
                replace(
                    record,
                    state=InstallState.ACTIVE,
                    last_event=LifecycleEvent.ACTIVATED,
                    last_detail={
                        "digest": plugin.digest,
                        "module": Path(module_path).name,
                        "dropped": list(verdict.dropped) if verdict else [],
                        "unused": list(verdict.unused) if verdict else [],
                    },
                )
            )
            return plugin

    @_in_flight
    async def revoke(self, plugin_id: str, *, reason: str = "revoked by user") -> RevocationReport:
        """Withdraw the approval and delete the grants. Do not claim to stop work.

        The linearisation point is *admission*, and this method is built around
        it. Work is in one of three places when a revocation lands:

        * **queued** — never admitted, so it is removed from the queue and refused;
          it will never run. Counted by ``queued_stopped``;
        * **admitted and running** — not interrupted: this host has no preemption,
          and saying otherwise would be the exact lie Phase 2.4 removed. Counted
          by ``running`` / ``still_running`` with ``stopped=False``;
        * **arriving** — already past authorisation, not yet admitted. The
          executor's per-plugin block refuses those until an approval is in force
          again.

        Ordering: the record first, then the executor drain, then the grants.

        * If the record cannot be written, nothing else happens at all — no
          queued call is refused, no grant is touched — and the caller gets an
          exception instead of a report that looks like success.
        * Writing the record and draining the queue happen with no ``await``
          between them, so the event loop cannot run anything in the gap: once
          the record says ``revoked``, the queue is already drained.
        * Deleting the grants comes last. If that fails, the record and the
          dispatch guard already refuse the calls a leftover grant would allow,
          and the failure is reported as ``plugin_lifecycle`` with
          ``revocation_persisted=True`` / ``grants_withdrawn=False`` rather than
          escaping as whatever the authority happened to raise.

        Re-running this verb on a record that is already ``revoked`` is a
        **cleanup**, not a second decision (Phase 2.6.8b, C1). It is accepted
        precisely so the paragraph above has a remedy: the retry re-writes the
        same decision, drains what is queued, and withdraws the grants that are
        still there. It authorises nothing — no approval is created, no grant is
        issued, and the block is left exactly as it was found. Lifting it is
        ``approve``'s job, and only for a capability set that is not empty.
        """
        self._require_open()
        async with self._lock:
            await self._refresh()
            record = self._records.get(plugin_id)
            if record is None:
                raise plugin_error(
                    PluginNotFoundError, f"plugin {plugin_id!r} is not known", plugin_id=plugin_id
                )
            self._check_transition(record, InstallState.REVOKED)
            was_active = record.state is InstallState.ACTIVE
            self._revoking.add(plugin_id)
            # Before the record, deliberately. The block refuses arrivals; the
            # *mark* stops another lifecycle's guard from lifting that block by
            # reading the record this method has not written yet. `_revoking` is
            # per instance and cannot do that (Phase 2.6.4 F1).
            blocked_before = self._host.executor.is_revoked(plugin_id)
            withdrawal = self._host.executor.begin_withdrawal(plugin_id)
            persisted = False
            try:
                # Read before the write, because the audit record is written in the
                # same step that takes the decision. Both counters are answers to
                # *different* questions on purpose — "already running" is not the
                # same as "waiting to start", and only one of them can be refused.
                running = self._host.executor.outstanding(plugin_id)
                queued = self._host.executor.queued_for(plugin_id)
                try:
                    await self._save(
                        replace(
                            record,
                            state=InstallState.REVOKED,
                            last_event=LifecycleEvent.REVOKED,
                            last_detail={
                                "reason": reason,
                                "was_active": was_active,
                                "queued_stopped": queued,
                                "still_running": running,
                                "stopped": False,
                            },
                        )
                    )
                except Exception as exc:
                    # The decision could not be stored, so it was not taken: a
                    # revoke that reports failure must not have half-happened.
                    raise plugin_error(
                        PluginLifecycleError,
                        "the revocation could not be recorded; the plugin keeps its "
                        "approval and its grants, and nothing was stopped",
                        plugin_id=plugin_id,
                        revocation_persisted=False,
                    ) from exc
                persisted = True
                drained = self._host.executor.revoke_plugin(plugin_id)
                try:
                    revoked = await self._permissions.revoke(principal_for(plugin_id))
                except PluginError:
                    raise
                except Exception as exc:
                    # The record says ``revoked`` and the block is set, so a
                    # leftover grant authorises nothing: the dispatch guard asks the
                    # record first. Reported rather than swallowed, because the
                    # decision and the grants store now disagree and the caller has
                    # to know which half happened — ``uninstall`` reports this exact
                    # pair the same way. ``revoked → revoked`` is legal, so the
                    # remedy is stated in the message: run the verb again.
                    raise plugin_error(
                        PluginLifecycleError,
                        "the revocation was recorded, but its grants could not be "
                        "withdrawn; the record and the block already refuse the plugin, "
                        "and running revoke again finishes the cleanup",
                        plugin_id=plugin_id,
                        revocation_persisted=True,
                        grants_withdrawn=False,
                    ) from exc
            finally:
                self._host.executor.end_withdrawal(withdrawal)
                if not persisted and not blocked_before:
                    # The decision was not stored, so it was not taken: the block
                    # this call placed was ours alone and is removed again. A
                    # block that was already there (an earlier revocation) stays,
                    # because this call has no authority over it.
                    self._host.executor.allow_plugin(plugin_id)
                self._revoking.discard(plugin_id)
        return RevocationReport(
            plugin_id=plugin_id,
            revoked_grants=tuple(revoked),
            was_active=was_active,
            still_running=drained.running,
            queued_stopped=drained.queued_stopped,
            running=drained.running,
            stopped=False,
        )

    def _remove_pin(self, record: LifecycleRecord, plugin_id: str) -> str:
        """Delete the pin only if it is still the one this record describes.

        Returns ``removed`` / ``absent`` / ``replaced`` / ``unreadable`` /
        ``no_record``, so the caller's log says which of those happened instead of
        one generic warning.

        Identity is ``(plugin_id, wasm_sha256)`` read from the file itself: both
        fields are written by :func:`install` from the manifest and from the bytes
        it hashed, so a different build, a different plugin, a hand-edited pin or a
        symlink is a mismatch and the file is **kept**. Deleting whatever happens
        to sit at the path would destroy a newer install in the name of an older
        decision (Phase 2.6.7b, B; measured in 2.6.7a).

        What this cannot do: the read and the ``unlink`` are two syscalls, so a
        writer that replaces the file between them still loses it. The check
        detects a replacement that has already happened; it is not an atomic
        guarantee against arbitrary writers. Cooperating writers are excluded by
        the store lock, which the caller holds for the whole operation.
        """
        if not record.manifest_path:
            return "no_record"
        pin_path = Path(record.manifest_path).parent / INSTALL_RECORD_NAME
        try:
            if pin_path.is_symlink():
                self._logger.warning(
                    "kept the install record for %s: it is a symbolic link, not the pin "
                    "this uninstall removed (%s)",
                    plugin_id,
                    pin_path.name,
                )
                return "replaced"
            raw = json.loads(pin_path.read_bytes())
        except FileNotFoundError:
            return "absent"
        except (OSError, ValueError) as exc:
            self._logger.warning(
                "kept the install record for %s: its identity cannot be read (%s), so it "
                "is not this operation's file to delete",
                plugin_id,
                exc,
            )
            return "unreadable"
        if not isinstance(raw, dict) or (
            raw.get("plugin_id") != plugin_id or raw.get("wasm_sha256") != record.wasm_sha256
        ):
            self._logger.warning(
                "kept the install record for %s: it describes a different install "
                "(plugin=%r digest=%r, expected plugin=%r digest=%r)",
                plugin_id,
                raw.get("plugin_id") if isinstance(raw, dict) else None,
                str(raw.get("wasm_sha256") if isinstance(raw, dict) else None)[:12],
                plugin_id,
                record.wasm_sha256[:12],
            )
            return "replaced"
        try:
            pin_path.unlink()
        except FileNotFoundError:
            return "absent"
        except OSError as exc:
            # The record already says uninstalled, so this pin is inert; a silent
            # ``suppress`` would hide a file the operator expects to be gone.
            self._logger.warning(
                "could not remove the install record for %s: %s",
                plugin_id,
                exc,
            )
            return "error"
        return "removed"

    @_in_flight
    async def uninstall(self, plugin_id: str, *, force: bool = False) -> LifecycleRecord:
        """Remove pin, approval and grants. Never deletes the module file.

        Refuses while host operations belonging to this plugin are still running
        unless ``force=True``, because "uninstalled" next to a running operation is
        a contradiction. The file is left on disk: this tool does not delete the
        user's files, and a half-deleted plugin directory is worse than a stale one.

        The record is **kept**, as an explicit tombstone. Deleting it would make
        "this was uninstalled" indistinguishable from "this was never seen here",
        which is the difference an operator needs when a directory is handed to
        them. Nothing in it authorises anything: the approval is dropped, the
        capability list is emptied, and the state is ``uninstalled``.

        Ordering, and why it is this way round (Phase 2.6.6 — it used to be the
        other way, and a failed write left the pin deleted under a record that
        still said *approved*):

        1. **admission** — drain the queue and block new calls, under the
           withdrawal mark, before anything is persisted;
        2. **record** — the decision. If this write fails, nothing has been
           removed: the pin is still there, the grants are still there, and the
           record still describes the build that is installed;
        3. **grants** — withdrawn only after the record refuses the plugin, so a
           failure here leaves *extra* grants that no approval covers rather than
           a record claiming a removal that did not happen;
        4. **pin** — deleted last, and only if the file still describes *this*
           install (``plugin_id`` and ``wasm_sha256``). A pin that was replaced
           belongs to whoever wrote it and is kept, with a warning naming what was
           found; a pin with no record to compare against is also kept. A pin left
           behind is inert: activation requires an ``approved`` record.

        Re-running an uninstall that already recorded ``uninstalled`` is a
        *cleanup*, not a new decision. The first attempt can fail after the
        tombstone was written — the grants store can be down, or the pin can have
        been replaced and therefore kept — and without that edge the only way to
        finish would be ``install`` followed by ``uninstall``, which needs the
        module to still be on disk. The record's approval and capability list are
        already empty, so the retry cannot authorise anything; it only retries the
        two removals, and the pin check still refuses to delete a stranger.

        The three stores (approval store, grants, pin file) cannot share a
        transaction, so this is not claimed to be atomic: each step's failure is
        reported with the state it left behind, in :class:`PluginLifecycleError`.
        """
        self._require_open()
        async with self._lock:
            await self._refresh()
            record = self._records.get(plugin_id)
            if record is None:
                raise plugin_error(
                    PluginNotFoundError, f"plugin {plugin_id!r} is not known", plugin_id=plugin_id
                )
            self._check_transition(record, InstallState.UNINSTALLED)
            outstanding = self._host.executor.outstanding(plugin_id)
            if outstanding and not force:
                raise plugin_error(
                    PluginLifecycleError,
                    f"plugin {plugin_id!r} still has {outstanding} operation(s) running; "
                    "they cannot be stopped by uninstalling",
                    plugin_id=plugin_id,
                    still_running=outstanding,
                )
            self._revoking.add(plugin_id)
            # The window where the record still says "approved" is the one a
            # guard in another lifecycle would lift the block inside (Phase 2.6.4
            # F1). The shared mark closes it, and the token makes sure only this
            # call can close *it* — a concurrent withdrawal that fails must not
            # clear the mark on its way out (Phase 2.6.5 audit, finding A).
            withdrawal = self._host.executor.begin_withdrawal(plugin_id)
            try:
                self._host.executor.revoke_plugin(plugin_id)
                try:
                    uninstalled = await self._save(
                        replace(
                            record,
                            state=InstallState.UNINSTALLED,
                            approval=None,
                            previous_approval=None,
                            capabilities=(),
                            last_event=LifecycleEvent.UNINSTALLED,
                            last_detail={"still_running": outstanding, "forced": force},
                        )
                    )
                except Exception as exc:
                    # The decision could not be stored, so it was not taken: a
                    # failed uninstall must not have half-happened. Nothing below
                    # this point has run yet, so the pin and the grants are intact.
                    raise plugin_error(
                        PluginLifecycleError,
                        "the uninstall could not be recorded; the plugin keeps its "
                        "approval, its pin and its grants",
                        plugin_id=plugin_id,
                        uninstall_persisted=False,
                    ) from exc
                try:
                    await self._permissions.revoke(principal_for(plugin_id))
                except PluginError:
                    raise
                except Exception as exc:
                    # The record already refuses the plugin, so a leftover grant
                    # authorises nothing (the dispatch guard asks the record
                    # first). Reported, not swallowed: the grants store and the
                    # decision now disagree, and an operator has to know.
                    raise plugin_error(
                        PluginLifecycleError,
                        "the plugin was uninstalled, but its grants could not be withdrawn; "
                        "the record refuses it, and no approval is left to re-grant from",
                        plugin_id=plugin_id,
                        uninstall_persisted=True,
                        grants_withdrawn=False,
                    ) from exc
                # Only the pin this record describes. A file written by something
                # else belongs to that something else: deleting it would destroy a
                # newer install in the name of an older decision, and the store
                # lock does not stop a non-cooperating writer (Phase 2.6.7b, B).
                self._remove_pin(record, plugin_id)
                return uninstalled
            finally:
                self._host.executor.end_withdrawal(withdrawal)
                self._revoking.discard(plugin_id)

    # -- read-only -------------------------------------------------------------

    async def inspect(self, plugin_id: str) -> Inspection:
        """Report requested, approved and granted side by side. No side effects.

        Reads the manifest and the module to answer "would activation accept this
        build right now", which is the question an operator actually has. It does
        not load, instantiate or call anything.

        It does read the store on every call, because the answer is about the
        present: a cached record would report a plugin as approved for as long as
        this process happens to be running.
        """
        async with self._lock:
            await self._refresh()
            record = self._records.get(plugin_id)
        if record is None:
            raise plugin_error(
                PluginNotFoundError, f"plugin {plugin_id!r} is not known", plugin_id=plugin_id
            )
        granted, notes = await self._grants_for(plugin_id)
        declared: str | None = None
        actual: str | None = None
        drift: ApprovalCheck | None = None
        pin_state = "not-checked"

        manifest_path = Path(record.manifest_path) if record.manifest_path else None
        manifest = _read_manifest(manifest_path) if manifest_path is not None else None
        module_path = (
            manifest_path.parent / record.module_name
            if manifest_path is not None and record.module_name
            else None
        )
        if manifest is None:
            notes += (
                "the manifest could not be read; requested capabilities are the recorded ones",
            )
        else:
            declared = manifest.wasm_sha256
        if module_path is not None and module_path.is_file():
            try:
                actual, _size = sha256_file(module_path)
            except PluginError as exc:
                notes += (f"the module could not be hashed: {exc.message}",)
            if manifest_path is not None:
                pin_state = (
                    "pinned-and-matching"
                    if (manifest_path.parent / INSTALL_RECORD_NAME).is_file()
                    and actual == record.wasm_sha256
                    else (
                        "pinned-but-modified"
                        if (manifest_path.parent / INSTALL_RECORD_NAME).is_file()
                        else "record-missing"
                    )
                )
        elif module_path is not None:
            pin_state = "module-missing"

        if record.approval is not None and manifest is not None and actual is not None:
            drift = check_approval(record.approval, manifest, actual)

        approved = tuple(sorted(record.approval.capabilities)) if record.approval else ()
        in_force = (
            record.approval is not None
            and record.state in (InstallState.APPROVED, InstallState.ACTIVE)
            and drift is not None
            and drift.ok
        )
        if record.approval is not None and not in_force:
            # The distinction this file exists for: an approval on file is
            # history, not authority. Say which one this is.
            notes += (
                f"the approval on file is historical: the plugin is {record.state.value}, so "
                "it authorises nothing",
            )
        if record.state is InstallState.UNINSTALLED:
            notes += ("the plugin was uninstalled; this record is a tombstone",)
        if in_force and approved != granted:
            # Two sets that must agree, reported rather than reconciled silently:
            # "approved" is what the store says a person decided, "granted" is what
            # dispatch will honour, and a difference means one of them is stale.
            notes += (
                f"approved and granted disagree: approved={list(approved)} granted={list(granted)}",
            )

        return Inspection(
            plugin_id=plugin_id,
            state=record.state,
            version=record.version,
            interface_version=record.interface_version,
            module_name=record.module_name,
            declared_sha256=declared,
            actual_sha256=actual,
            approved_sha256=record.approval.wasm_sha256 if record.approval else None,
            pin_state=pin_state,
            requested=tuple(sorted(record.capabilities)),
            approved=approved,
            granted=granted,
            drift=drift,
            approval_in_force=in_force,
            last_event=record.last_event,
            last_detail=record.last_detail,
            notes=notes,
        )

    async def _grants_for(self, plugin_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        rows, enumerable = await self._grant_rows()
        if not enumerable:
            return (), ("this authority cannot list grants; `granted` is unknown, not empty",)
        principal = principal_for(plugin_id)
        names = tuple(
            sorted({str(row["capability"]) for row in rows if row.get("adapter_id") == principal})
        )
        return names, ()


def _base_name(path: str) -> str:
    """Basename of a declared module path, for display only."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _installation_mismatch(
    record: LifecycleRecord,
    installed: InstallRecord,
    manifest: WasmPluginManifest,
    manifest_path: Path,
) -> str | None:
    """Why this install is *not* the one ``record`` describes; ``None`` if it is.

    ``integrity.install`` returns ``"unchanged"`` when the *pin* matches the bytes
    on disk. That is not yet the same statement as "the lifecycle record
    describes this installation": the record also carries the ABI version, the
    module name, the capability set as recorded, and where the manifest lives.
    All of them are compared before anything is skipped, because a short-circuit
    that guessed would silently keep a stale record.

    Two sources, used for different questions: the pin answers "are these the
    bytes that were installed", and the freshly parsed ``manifest`` answers "what
    does the file on disk say *now*". They disagree in exactly one place —
    ``interface_version`` is absent from the pin's own ``unchanged`` test — and
    that is why the ABI version is read from the manifest. The pin cannot be the
    authority for it: a build that kept its bytes and its version while changing
    its ABI leaves the pin describing the old interface.

    The state is checked too: a plugin that was never installed, or that was
    uninstalled, has to be written, or it would stay invisible to activation.

    Paths are compared **canonically**, not as strings. ``/p/a.json`` and
    ``a.json`` (run from ``/p``) are the same file; comparing the strings made
    the second one look like a different installation, which took the full write
    path and cleared a live approval while reporting ``unchanged``. When the
    canonical form cannot be established, this says "different" — the caller then
    does the conservative full write instead of claiming nothing happened.

    Returning the reason rather than a bool is deliberate: the full path records
    it, so "the pin said unchanged and the record was rewritten anyway" is
    legible in ``last_detail`` instead of looking like a contradiction.
    """
    if record.state in (InstallState.DISCOVERED, InstallState.UNINSTALLED):
        return f"the record is {record.state.value}"
    if not record.manifest_path:
        return "the record has no manifest path"
    try:
        same_manifest = _canonical_path(record.manifest_path) == _canonical_path(manifest_path)
    except (OSError, ValueError):  # a path this platform cannot canonicalise
        return "the manifest path could not be canonicalised"
    if not same_manifest:
        return "the manifest lives in a different directory"
    if record.plugin_id != installed.plugin_id:
        return "the plugin id changed"
    if record.version != manifest.version:
        return "the version changed"
    if record.interface_version != manifest.interface_version:
        return "the ABI version changed"
    if record.wasm_sha256 != installed.wasm_sha256:
        return "the module content changed"
    if record.module_name != _base_name(installed.wasm_path):
        return "the module file name changed"
    if tuple(sorted(record.capabilities)) != tuple(sorted(manifest.declared_capabilities)):
        return "the requested capability set changed"
    return None


__all__ = [
    "APPROVAL_RECORD_VERSION",
    "ActivationGuard",
    "ApprovalCheck",
    "ApprovalStore",
    "CapabilityAuthority",
    "DisclosedPlugin",
    "Inspection",
    "InstallState",
    "JsonApprovalStore",
    "LifecycleEvent",
    "LifecycleRecord",
    "MemoryApprovalStore",
    "PluginApproval",
    "PluginLifecycle",
    "RevocationReport",
    "check_approval",
    "discover",
]
