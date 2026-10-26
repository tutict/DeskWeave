"""Execution governance for host capabilities.

Phase 2.3 left one honest hole. ``HostCapability.run`` is synchronous, so the
capability executed on the event loop thread: a slow capability froze every other
coroutine, and ``future.cancel()`` only stopped the *caller* from waiting — never
the work. This module is the smallest mechanism that fixes both, without turning
the runtime into a job scheduler.

Four things are kept apart on purpose, because collapsing them is how a timeout
gets reported as a cancellation:

``completed``
    The capability returned a value.
``timed_out``
    The caller's budget expired. The operation may still be running, and for a
    synchronous capability it certainly is.
``cancelled``
    The operation stopped, and we know because the coroutine finished or raised
    ``CancelledError``.
``cancellation_requested``
    We asked. The grace period expired without the operation stopping. This is
    *not* a cancellation and is never recorded as one.

Which state is even possible depends on the capability:

* a plain ``def run`` runs in a **bounded** thread pool. It can be timed out but
  never cancelled — no Python API can interrupt a running function;
* an ``async def run`` runs on the loop and *is* cancellable, because
  ``Task.cancel()`` raises at its next await;
* either way the slot is released when the operation actually finishes, not when
  the caller stops waiting. That is the difference between "the caller gave up"
  and "the resource is free".

What this is not: a distributed scheduler, a retry engine, or a work-stealing
pool. Phase 2.5 added one bounded queue with round-robin admission, because
refuse-when-full has a denial-of-service shape: one plugin that keeps four calls
in flight can starve every other plugin forever without ever exceeding its own
quota. The queue is small, the wait is capped, and when either bound is reached
the answer is a refusal — never an unbounded backlog.

Fairness here is **bounded, not strict**: a queued call is admitted as soon as a
slot frees and its turn comes in the round-robin order, so its wait is bounded
by one in-flight operation plus the queue timeout. It is not a latency
guarantee, and it is not weighted by anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .errors import PluginLifecycleError, plugin_error

#: Cost of a capability that does not declare one. One unit per call, which makes
#: a plain cost budget behave exactly like a call budget until a capability says
#: otherwise.
DEFAULT_CAPABILITY_COST = 1

#: Cost budget used when a caller dispatches without a scope. Matches the manifest
#: default so a directly-driven registry behaves like an ordinary invocation
#: instead of refusing everything for lack of a budget.
DEFAULT_SCOPE_COST = 64

#: How many calls may wait for a slot at once. Small on purpose: a queue is a
#: latency buffer, not a backlog. Zero disables queueing entirely, which is how a
#: host asks for Phase 2.4's refuse-fast behaviour.
DEFAULT_MAX_WAITERS = 16

#: Longest a call may wait for a slot. This is the bound that makes "plugin B
#: gets a turn" a testable statement instead of a hope.
DEFAULT_MAX_QUEUE_WAIT_S = 0.25

#: Window over which rate limits are counted.
DEFAULT_RATE_WINDOW_S = 1.0

#: Ceiling on any configured rate. A capability cannot declare a rate so high
#: that the limiter stops resembling a limiter.
MAX_RATE_PER_S = 1_000.0


class CallState(StrEnum):
    """What actually happened to a host call, as opposed to what was asked."""

    #: Never executed: refused by cost, concurrency, rate or shutdown. Nothing
    #: was charged.
    REFUSED = "refused"
    #: The capability returned a value.
    COMPLETED = "completed"
    #: The capability raised.
    FAILED = "failed"
    #: The caller's budget expired; the operation was not stopped.
    TIMED_OUT = "timed_out"
    #: The operation stopped, confirmed.
    CANCELLED = "cancelled"
    #: Cancellation asked for, not confirmed.
    CANCELLATION_REQUESTED = "cancellation_requested"


#: States in which the operation is known to have stopped. Only these may free
#: the concurrency slot at the moment the caller learns about them.
STOPPED_STATES = frozenset({CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED})


class Refusal(StrEnum):
    """Why a call never reached execution."""

    COST = "cost"
    CONCURRENCY = "concurrency"
    #: The bounded queue was already full.
    QUEUE_FULL = "queue_full"
    #: The bounded wait expired before a slot became available.
    QUEUE_TIMEOUT = "queue_timeout"
    #: A per-plugin or per-capability rate limit refused it.
    RATE = "rate"
    CANCELLED = "cancelled"
    SHUTDOWN = "shutdown"
    #: The plugin's approval was withdrawn. Distinct from :attr:`SHUTDOWN` and
    #: from a cost or capacity refusal because the caller's next step differs:
    #: nothing about this plugin may run again until it is approved afresh.
    REVOKED = "revoked"


#: Map an admission status to the refusal it becomes once it leaves the queue.
_REFUSAL_FOR: dict[str, Refusal] = {
    "cost": Refusal.COST,
    "capacity": Refusal.CONCURRENCY,
    "cancelled": Refusal.CANCELLED,
    "closed": Refusal.SHUTDOWN,
    "revoked": Refusal.REVOKED,
}


class Runnable(Protocol):
    """The only part of a capability this module needs."""

    name: str

    def run(self, payload: Mapping[str, Any]) -> Any:
        """Perform the work. A coroutine function is treated as cancellable."""
        ...


@dataclass
class ExecutionScope:
    """Per-invocation accounting: one cost budget, one concurrency count.

    Created by ``WasmPluginHost.invoke`` and shared by every host call in that
    invocation. A failed invocation's scope is discarded with it, which is why a
    cost budget can never leak into the next call.
    """

    plugin_id: str
    max_cost: int
    cost_spent: int = 0
    charges: int = 0
    live: int = 0
    aborted: bool = False

    @property
    def cost_left(self) -> int:
        return max(0, self.max_cost - self.cost_spent)


@dataclass(frozen=True)
class ExecutionResult:
    """One governed execution, with the state that the audit will report."""

    state: CallState
    data: dict[str, Any] | None = None
    error: BaseException | None = None
    cost: int = 0
    reason: str = ""
    elapsed_ms: float = 0.0
    #: ``False`` when the operation is still running. Never inferred — a timeout
    #: does not set this to ``True``.
    confirmed_stopped: bool = True
    refusal: Refusal | None = None

    @property
    def executed(self) -> bool:
        """Whether the capability actually ran, successfully or not."""
        return self.state in {
            CallState.COMPLETED,
            CallState.FAILED,
            CallState.TIMED_OUT,
            CallState.CANCELLED,
            CallState.CANCELLATION_REQUESTED,
        }


@dataclass(frozen=True)
class DrainReport:
    """What withdrawing one plugin's approval did to its *unstarted* work.

    ``queued_stopped`` counts calls that were waiting for a slot and were removed
    from the queue: they never ran, so nothing had to be stopped. ``running``
    counts calls that had already been admitted — they are **not** interrupted,
    which is why ``stopped`` is not a field here: this object never claims to
    have stopped anything that had started.
    """

    plugin_id: str
    queued_stopped: int
    running: int


@dataclass
class _Counters:
    """Live operation counts, guarded by one lock.

    All four are increment-on-admission, decrement-on-finish. Decrementing on
    timeout instead is the bug this class exists to make impossible.
    """

    global_count: int = 0
    blocking: int = 0
    per_plugin: dict[str, int] = field(default_factory=dict)

    def get(self, plugin_id: str) -> int:
        return self.per_plugin.get(plugin_id, 0)

    def acquire(self, plugin_id: str, *, blocking: bool) -> None:
        self.global_count += 1
        if blocking:
            self.blocking += 1
        self.per_plugin[plugin_id] = self.get(plugin_id) + 1

    def release(self, plugin_id: str, *, blocking: bool) -> None:
        self.global_count = max(0, self.global_count - 1)
        if blocking:
            self.blocking = max(0, self.blocking - 1)
        left = self.get(plugin_id) - 1
        if left <= 0:
            self.per_plugin.pop(plugin_id, None)
        else:
            self.per_plugin[plugin_id] = left


@dataclass
class _Waiter:
    """One call parked in the admission queue.

    The future resolves with ``None`` when the call is granted (the slot has
    already been charged by then) or with an :class:`ExecutionResult` when it is
    refused. It never resolves with "try again": a waiter is woken exactly once,
    which is what keeps the round-robin order meaningful instead of turning
    every release into a thundering herd.
    """

    plugin_id: str
    capability: str
    cost: int
    is_async: bool
    scope: ExecutionScope
    future: asyncio.Future[Any]


class Withdrawal:
    """Ownership token for one in-flight withdrawal of one plugin.

    A withdrawal mark without an owner is a mark that anyone can clear by
    accident: Phase 2.6.4 shipped the mark as a plain set, so a second
    withdrawal whose store write failed cleared the *first* withdrawal's mark in
    its ``finally``, and a guard then lifted the block while an uninstall was
    still between steps (measured in the 2.6.5 audit). The token is what makes
    "end the withdrawal" mean "end *my* withdrawal": :meth:`RevocationBarrier.
    end_withdrawal` only removes the mark this token was issued for.

    Identity, not value: two withdrawals of one plugin are distinct even when
    they carry the same ``plugin_id``, so ``==`` and hashing are the default
    object ones on purpose.
    """

    __slots__ = ("plugin_id", "serial")

    def __init__(self, plugin_id: str, serial: int) -> None:
        self.plugin_id = plugin_id
        #: Monotonic within one barrier. Diagnostics only — the token is
        #: identified by object identity, never by this number.
        self.serial = serial

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"Withdrawal(plugin_id={self.plugin_id!r}, serial={self.serial})"


class RevocationBarrier:
    """Plugins whose admission is blocked, shared by every executor serving one store.

    A ``HostExecutor`` owning this set privately is only correct while there is
    exactly one executor per approval store. Phase 2.6.2 shipped that assumption
    without checking it: two lifecycles over one store each had their own set, so
    a revocation in one drained *its* queue and left the other's queued calls
    admissible. The record (the approval store) is the shared truth, so the block
    derived from it has to be shared too.

    Deliberately a plain ``threading.Lock``: the check runs inside
    ``HostExecutor._lock`` and never awaits, so it must not be an ``asyncio``
    primitive. There is no path from here back into the executor, so the lock
    order is one-way.

    Scope: this is process memory. Two *processes* on one store still have
    separate barriers, which is the same limitation as before and is documented.

    Each block also increments a per-plugin **generation**. The dispatch guard
    reads a record, decides "approved", and only then lifts the block — and that
    decision can be stale: a revocation committed in between would be undone by
    the lift. Comparing the generation captured *before* the read closes it
    (`lift_if_unchanged`), and the generation only ever moves forward.

    A withdrawal is not only a block. ``revoke()`` writes the record before it
    blocks, so the record is already the truth when the block lands; but
    ``uninstall()`` has to write last (it removes a pin and grants first), and in
    that window the record still says *approved*. A guard in another lifecycle
    would then read a true-but-superseded record and lift the block that was just
    placed. ``begin_withdrawal`` therefore marks the plugin as **in the middle of
    a withdrawal** in this shared barrier, and both lift paths refuse while that
    mark is set. The mark is process-scoped like the rest of the barrier, so it
    does not close the cross-process gap — nothing here does.

    The mark is **owned**: ``begin_withdrawal`` returns a :class:`Withdrawal`
    token and ``end_withdrawal`` needs that token, so a withdrawal that fails
    cannot clear a mark another withdrawal is still holding. A copied mark has no
    owner and could never be released, so :meth:`HostExecutor.adopt_barrier`
    deliberately carries blocks but not marks.
    """

    __slots__ = (
        "__weakref__",
        "_blocked",
        "_lock",
        "_revocations",
        "_serial",
        "_withdrawing",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocked: set[str] = set()
        self._revocations: dict[str, int] = {}
        #: plugin_id -> the marks currently held for it. More than one entry
        #: means more than one withdrawal is in flight for the same plugin, and
        #: each of them can only release its own.
        self._withdrawing: dict[str, set[Withdrawal]] = {}
        self._serial = 0

    def block(self, plugin_id: str) -> None:
        with self._lock:
            self._blocked.add(plugin_id)
            self._revocations[plugin_id] = self._revocations.get(plugin_id, 0) + 1

    def begin_withdrawal(self, plugin_id: str) -> Withdrawal:
        """Block, mark as *being withdrawn*, and return the ownership token.

        The returned token is the only way to end this mark. The generation is
        bumped as well, so a guard that captured it before this call cannot lift
        the block it places (``lift_if_unchanged``).
        """
        with self._lock:
            self._serial += 1
            withdrawal = Withdrawal(plugin_id, self._serial)
            self._blocked.add(plugin_id)
            self._withdrawing.setdefault(plugin_id, set()).add(withdrawal)
            self._revocations[plugin_id] = self._revocations.get(plugin_id, 0) + 1
            return withdrawal

    def end_withdrawal(self, withdrawal: Withdrawal) -> bool:
        """Release *this* withdrawal's mark. ``False`` means it was not ours.

        The record is written (or the attempt failed); this only stops refusing
        lifts on behalf of one operation. A token that was already released, or
        that belongs to a different barrier, releases nothing — and in particular
        cannot clear a mark another withdrawal still holds. The block itself
        stays: ``approve`` lifts it on a new decision, and the dispatch guard
        lifts it when it reads an approval that is still in force.
        """
        with self._lock:
            marks = self._withdrawing.get(withdrawal.plugin_id)
            if not marks or withdrawal not in marks:
                return False
            marks.discard(withdrawal)
            if not marks:
                del self._withdrawing[withdrawal.plugin_id]
            return True

    def withdrawing(self, plugin_id: str) -> bool:
        with self._lock:
            return bool(self._withdrawing.get(plugin_id))

    def withdrawing_snapshot(self) -> frozenset[str]:
        """Plugins with at least one withdrawal in flight. For diagnostics."""
        with self._lock:
            return frozenset(self._withdrawing)

    def lift(self, plugin_id: str) -> bool:
        """Lift unconditionally, unless a withdrawal is in progress.

        ``False`` means the block was kept because someone is in the middle of
        withdrawing this plugin: a person clicking "approve" concurrently with an
        uninstall does not get to resurrect it mid-way. The next guarded call
        lifts it once the withdrawal has finished.
        """
        with self._lock:
            if self._withdrawing.get(plugin_id):
                return False
            self._blocked.discard(plugin_id)
            return True

    def generation(self, plugin_id: str) -> int:
        """How many times this plugin has been blocked. Monotonic."""
        with self._lock:
            return self._revocations.get(plugin_id, 0)

    def lift_if_unchanged(self, plugin_id: str, observed: int) -> bool:
        """Lift the block only if no revocation landed since ``observed``.

        ``False`` means a revocation committed after the caller captured the
        generation, so whatever it read about the approval is out of date and the
        block must stay. The caller is expected to refuse; the block is the
        backstop, not the answer.

        An unconditional :meth:`lift` is still right for ``approve()``: a person
        just said yes, and that decision is newer than any revocation. This
        conditional form is for the *guard*, whose "yes" was computed from an
        older read.
        """
        with self._lock:
            if self._withdrawing.get(plugin_id):
                return False
            if self._revocations.get(plugin_id, 0) != observed:
                return False
            self._blocked.discard(plugin_id)
            return True

    def blocks(self, plugin_id: str) -> bool:
        with self._lock:
            return plugin_id in self._blocked

    def snapshot(self) -> frozenset[str]:
        """The currently blocked plugins. For tests and for adopting a barrier."""
        with self._lock:
            return frozenset(self._blocked)


class HostExecutor:
    """Runs capabilities under admission control, with honest cancellation.

    The defaults are small on purpose. They are a statement about what this host
    is willing to do at once, not a tuning knob: raising them without measuring
    is how a sandbox turns into a load generator.

    Admission order is: shutdown, cancellation, **rate**, capacity, cost. Rate
    comes before capacity so that a flooding plugin is refused cheaply rather
    than occupying queue space that a well-behaved plugin could use.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 8,
        blocking_workers: int = 4,
        max_outstanding_per_plugin: int = 4,
        max_concurrent_per_invocation: int = 2,
        cancellation_grace_s: float = 0.25,
        max_waiters: int = DEFAULT_MAX_WAITERS,
        max_queue_wait_s: float = DEFAULT_MAX_QUEUE_WAIT_S,
        default_rate_per_s: float | None = None,
        rate_window_s: float = DEFAULT_RATE_WINDOW_S,
        clock: Callable[[], float] | None = None,
        revocation_barrier: RevocationBarrier | None = None,
    ) -> None:
        if max_concurrent < 1 or blocking_workers < 1:
            raise ValueError("host concurrency limits must be at least 1")
        if blocking_workers > max_concurrent:
            raise ValueError("blocking_workers cannot exceed max_concurrent")
        if max_waiters < 0:
            raise ValueError("max_waiters cannot be negative")
        if max_waiters > 0 and max_queue_wait_s <= 0:
            raise ValueError("a queue with max_waiters > 0 needs max_queue_wait_s > 0")
        if default_rate_per_s is not None and not 0 < default_rate_per_s <= MAX_RATE_PER_S:
            raise ValueError(f"default_rate_per_s must be in (0, {MAX_RATE_PER_S}]")
        if not 0 < rate_window_s <= 60:
            raise ValueError("rate_window_s must be in (0, 60]")

        self.max_concurrent = max_concurrent
        self.blocking_workers = blocking_workers
        self.max_outstanding_per_plugin = max_outstanding_per_plugin
        self.max_concurrent_per_invocation = max_concurrent_per_invocation
        self.cancellation_grace_s = cancellation_grace_s
        self.max_waiters = max_waiters
        self.max_queue_wait_s = max_queue_wait_s
        self.default_rate_per_s = default_rate_per_s
        self.rate_window_s = rate_window_s

        self._lock = threading.Lock()
        self._counters = _Counters()
        # Keyed by scope, so a cancelled invocation can find its own operations
        # and nothing else's. One plugin's cancellation must not interrupt
        # another's work.
        self._tasks: dict[asyncio.Task[Any], ExecutionScope] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._closed = False
        self._started = 0
        self._completed = 0
        self._refused = 0

        # Admission queue. ``_rr`` is the round-robin order of plugins that have
        # at least one waiter: a plugin is moved to the back after it is served,
        # so a backlog behind one plugin cannot bury another plugin's first call.
        self._waiters: dict[str, deque[_Waiter]] = {}
        self._rr: list[str] = []
        self._waiter_count = 0
        # Rate-limit windows, keyed by (plugin, capability). Separate keys are
        # what stop one plugin's flood from consuming another plugin's budget.
        self._rates: dict[tuple[str, str], deque[float]] = {}
        # Plugins whose approval was withdrawn. A call for one of these is
        # refused *before* it can be queued, which is the hard part of revoking:
        # a call that passed authorisation a moment earlier must not be admitted
        # afterwards just because it is already in flight through the host.
        # Cleared only by an explicit re-approval (`allow_plugin`).
        #
        # Held in a shared barrier rather than a private set: every executor
        # serving the same approval store must see the same block, or a queued
        # call in one host outlives a revocation committed by another.
        self._barrier = (
            revocation_barrier if revocation_barrier is not None else RevocationBarrier()
        )
        #: Whether the barrier above was chosen for this executor by a store.
        #: One executor serves one approval store: a second, *different* barrier
        #: would replace the first and silently orphan the plugins blocked under
        #: it, so the binding is one-way (see :meth:`adopt_barrier`).
        self._bound_barrier = revocation_barrier is not None
        self._clock = clock or time.monotonic

    # -- introspection ----------------------------------------------------
    def snapshot(self) -> dict[str, int]:
        """Live accounting, for tests and for the shutdown log."""
        with self._lock:
            return {
                "running": self._counters.global_count,
                "blocking": self._counters.blocking,
                "queued": self._waiter_count,
                "started": self._started,
                "completed": self._completed,
                "refused": self._refused,
            }

    def outstanding(self, plugin_id: str | None = None) -> int:
        """Operations not yet finished — including ones the caller gave up on."""
        with self._lock:
            if plugin_id is None:
                return self._counters.global_count
            return self._counters.get(plugin_id)

    def queued(self) -> int:
        """Calls currently waiting for a slot."""
        with self._lock:
            return self._waiter_count

    # -- revocation -------------------------------------------------------
    def revoke_plugin(self, plugin_id: str) -> DrainReport:
        """Refuse this plugin's unstarted work and block further admission.

        The linearisation point of a revocation is *admission*, and this is where
        it is implemented. Two things happen under one lock:

        * every waiter for ``plugin_id`` is removed from the queue and woken with
          a :attr:`Refusal.REVOKED` result, so a call that was parked before the
          decision can never start after it;
        * the plugin is added to the refused set, so a call that is still on its
          way in — past the permission check, not yet charged — is refused too.

        Calls that already hold a slot are left alone. There is no preemption in
        this host, and ``running`` says how many are in that position rather than
        pretending they stopped.
        """
        wakes: list[tuple[asyncio.Future[Any], Any]] = []
        with self._lock:
            self._barrier.block(plugin_id)
            queued_stopped = 0
            for waiter in [
                item for item in self._waiters.get(plugin_id, ()) if not item.future.done()
            ]:
                if self._remove_waiter_locked(waiter):
                    queued_stopped += 1
                    wakes.append(
                        (
                            waiter.future,
                            self._refusal_result(
                                waiter,
                                "revoked",
                                "the plugin's approval was withdrawn before this call was admitted",
                            ),
                        )
                    )
            running = self._counters.get(plugin_id)
        self._wake(wakes)
        return DrainReport(plugin_id=plugin_id, queued_stopped=queued_stopped, running=running)

    def allow_plugin(self, plugin_id: str) -> bool:
        """Lift the post-revocation block. Called only when a fresh approval exists.

        Deliberately not called by ``install``: re-pinning a module is not a
        decision about capabilities, and a plugin that was revoked must stay
        unable to run until a person approves it again.

        ``False`` means the lift was refused because a withdrawal is in progress;
        the block is then cleared by the next guarded call once it has finished.
        """
        with self._lock:
            return self._barrier.lift(plugin_id)

    def begin_withdrawal(self, plugin_id: str) -> Withdrawal:
        """Block *and* mark as being withdrawn, returning this call's token.

        Shared by every executor serving this store, which is the point: the
        per-instance ``_revoking`` set cannot stop a second lifecycle's guard
        from lifting a block it placed a moment ago. The token is what stops a
        *second* withdrawal from releasing the first one's mark.
        """
        with self._lock:
            return self._barrier.begin_withdrawal(plugin_id)

    def end_withdrawal(self, withdrawal: Withdrawal) -> bool:
        """Release one owned mark. ``False`` means this token held nothing here."""
        with self._lock:
            return self._barrier.end_withdrawal(withdrawal)

    def is_withdrawing(self, plugin_id: str) -> bool:
        with self._lock:
            return self._barrier.withdrawing(plugin_id)

    def is_revoked(self, plugin_id: str) -> bool:
        with self._lock:
            return self._barrier.blocks(plugin_id)

    def revocation_generation(self, plugin_id: str) -> int:
        """Snapshot the block generation — take this *before* reading the record.

        The dispatch guard uses it to make the lift conditional: see
        :meth:`RevocationBarrier.lift_if_unchanged`.
        """
        with self._lock:
            return self._barrier.generation(plugin_id)

    def lift_if_unchanged(self, plugin_id: str, observed: int) -> bool:
        """Lift the block only if no revocation landed since ``observed``."""
        with self._lock:
            return self._barrier.lift_if_unchanged(plugin_id, observed)

    @property
    def revocation_barrier(self) -> RevocationBarrier:
        """The shared block set. Two executors serving one store must share one."""
        return self._barrier

    def adopt_barrier(self, barrier: RevocationBarrier) -> None:
        """Use ``barrier`` from now on, carrying over anything already blocked.

        Only for construction time: a lifecycle resolves the barrier for its
        store and hands it to an executor the registry may have created earlier.
        Nothing is in flight at that point, so no call is observed half-moved
        between two sets.

        **An executor serves one store.** Adopting a *second, different* barrier
        is refused rather than applied: replacing the first would leave every
        plugin blocked under it unblocked, and a revocation committed through one
        store would stop blocking calls that belong to it. The alternative —
        keeping both sets and refusing if *any* of them blocks — is not
        implemented, because nothing in this package constructs such a pairing
        and an untested combined policy is worse than a stated refusal.

        **Blocks are carried, withdrawal marks are not.** A block is released by
        a decision (``approve``) or by a guard that reads an approval in force, so
        copying it preserves a refusal that outlives the move. A mark is released
        by the token that created it, and a copied mark would have no owner — it
        could never be ended, which is a permanent refusal dressed as a safe one.
        In the only adoption this package performs the source barrier is a fresh
        executor's private one, so there is nothing to carry either way; the loop
        exists for callers that adopt late.

        Carrying a block bumps the generation, so a guard in another lifecycle
        that captured the generation before this call refuses once
        (``lift_if_unchanged``). That is a temporary false refusal in the safe
        direction: one call is refused, and the next one sees the new generation.
        """
        with self._lock:
            if barrier is self._barrier:
                return
            if self._bound_barrier:
                raise plugin_error(
                    PluginLifecycleError,
                    "this executor is already bound to one approval store's revocation "
                    "barrier; an executor may serve only one store, because a second "
                    "barrier would replace the first and unblock the plugins it held",
                    executor=type(self).__name__,
                )
            for plugin_id in self._barrier.snapshot():
                barrier.block(plugin_id)
            self._barrier = barrier
            self._bound_barrier = True

    def queued_for(self, plugin_id: str) -> int:
        """Calls for one plugin currently waiting for a slot.

        Kept apart from :meth:`outstanding` on purpose: those two numbers answer
        different questions ("waiting to start" versus "started, still running")
        and merging them is what let a revocation report a queued call as if it
        were running work.
        """
        with self._lock:
            return len(self._waiters.get(plugin_id, ()))

    # -- lifecycle --------------------------------------------------------
    def _pool_or_create(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.blocking_workers,
                thread_name_prefix="dw-host-cap",
            )
        return self._pool

    def abort_scope(self, scope: ExecutionScope) -> int:
        """Cancel every cancellable operation belonging to ``scope``.

        Returns the number of tasks asked to stop. Synchronous operations are not
        in this set at all — they cannot be asked — so the return value is *not*
        "everything stopped". Callers must not present it as such.

        Calls still waiting in the admission queue are removed and refused: they
        never started, so there is nothing to stop, but leaving them to be
        admitted into a cancelled invocation would execute work nobody wants.
        """
        scope.aborted = True
        asked = 0
        for task, owner in list(self._tasks.items()):
            if owner is scope and not task.done():
                task.cancel()
                asked += 1

        wakes: list[tuple[asyncio.Future[Any], Any]] = []
        with self._lock:
            for plugin_id in list(self._rr):
                queue = self._waiters.get(plugin_id)
                if not queue:
                    continue
                for waiter in [item for item in queue if item.scope is scope]:
                    if self._remove_waiter_locked(waiter):
                        wakes.append(
                            (
                                waiter.future,
                                self._refusal_result(
                                    waiter, "cancelled", "the invocation was cancelled"
                                ),
                            )
                        )
        self._wake(wakes)
        return asked

    async def aclose(self) -> int:
        """Stop accepting work and return how many operations were still running.

        The pool is shut down **without waiting**: a blocking capability cannot be
        stopped, so ``shutdown(wait=True)`` would hang the host on a call it
        already gave up on. The returned count is the honest answer to "what did
        we leave behind".

        Queued calls are not left hanging either: each is woken with a shutdown
        refusal before this returns, so "what happened to my request" has an
        answer for every caller — running, queued, or already finished.
        """
        with self._lock:
            self._closed = True
            wakes: list[tuple[asyncio.Future[Any], Any]] = [
                (
                    waiter.future,
                    self._refusal_result(
                        waiter, "closed", "the host is shutting down before the call was admitted"
                    ),
                )
                for queue in self._waiters.values()
                for waiter in queue
            ]
            self._waiters.clear()
            self._rr.clear()
            self._waiter_count = 0
            outstanding = self._counters.global_count
        self._wake(wakes)

        for task in list(self._tasks):
            task.cancel()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=False)
            self._pool = None
        return outstanding

    # -- execution --------------------------------------------------------
    async def run(
        self,
        *,
        plugin_id: str,
        capability: str,
        handler: Runnable,
        payload: Mapping[str, Any],
        scope: ExecutionScope,
        timeout_s: float,
    ) -> ExecutionResult:
        """Admit and execute one host call.

        Admission order is shutdown, cancellation, rate, capacity, cost, then
        charge, then execute. The cost is charged *inside* the same critical
        section that admits the call, so an operation that is never admitted is
        never billed and a billed operation is always attempted exactly once.

        A call that is only blocked by capacity — not by cost or rate — waits in
        a bounded queue for a bounded time. That is the whole fairness mechanism:
        without it, a plugin holding every slot refuses everyone else forever
        while staying inside its own quota.
        """
        started = time.perf_counter()
        cost = max(0, int(getattr(handler, "cost", DEFAULT_CAPABILITY_COST)))
        rate_limit = getattr(handler, "rate_limit_per_s", None)
        is_async = inspect.iscoroutinefunction(handler.run)

        def result(state: CallState, **kwargs: Any) -> ExecutionResult:
            kwargs.setdefault("cost", cost)
            kwargs.setdefault("elapsed_ms", (time.perf_counter() - started) * 1000.0)
            return ExecutionResult(state=state, **kwargs)

        waiter: _Waiter | None = None
        admitted = False
        with self._lock:
            if self._closed:
                return self._refused_locked(result, "the host is shutting down", Refusal.SHUTDOWN)
            if scope.aborted:
                return self._refused_locked(
                    result, "the invocation was cancelled", Refusal.CANCELLED
                )
            if self._barrier.blocks(plugin_id):
                # Checked here, not only at dispatch: a call can be authorised a
                # moment before the revocation lands and reach this point after.
                # Admission is the last place where "it never started" is still
                # true, so it is the place that has to say no.
                return self._refused_locked(
                    result,
                    "the plugin's approval was withdrawn before this call was admitted",
                    Refusal.REVOKED,
                )
            rate_reason = self._rate_refusal_locked(plugin_id, capability, rate_limit)
            if rate_reason is not None:
                return self._refused_locked(result, rate_reason, Refusal.RATE)

            status, reason = self._admissible_locked(
                plugin_id=plugin_id, is_async=is_async, cost=cost, scope=scope
            )
            if status == "ok":
                self._charge_locked(plugin_id=plugin_id, is_async=is_async, cost=cost, scope=scope)
                admitted = True
            elif status != "capacity":
                return self._refused_locked(result, reason, _REFUSAL_FOR[status])
            elif self.max_waiters <= 0:
                return self._refused_locked(
                    result, reason + "; queueing is disabled", Refusal.CONCURRENCY
                )
            elif self._waiter_count >= self.max_waiters:
                return self._refused_locked(
                    result,
                    f"{reason}; the admission queue is already full at "
                    f"{self._waiter_count} waiter(s) (limit {self.max_waiters})",
                    Refusal.QUEUE_FULL,
                )
            else:
                waiter = self._enqueue_locked(
                    plugin_id=plugin_id,
                    capability=capability,
                    cost=cost,
                    is_async=is_async,
                    scope=scope,
                )

        if not admitted:
            assert waiter is not None
            queued_refusal = await self._await_turn(waiter, result)
            if queued_refusal is not None:
                return queued_refusal

        released = False
        slot_lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with slot_lock:
                if released:
                    return
                released = True
            with self._lock:
                self._counters.release(plugin_id, blocking=not is_async)
                self._completed += 1
                # ``scope.live`` is the per-invocation count. It goes down when
                # the operation finishes, not when the caller stops waiting, and
                # it goes down under the same lock admission reads it under.
                scope.live = max(0, scope.live - 1)
                wakes = self._dispatch_locked()
            self._wake(wakes)

        if is_async:
            return await self._run_async(
                handler=handler,
                payload=payload,
                scope=scope,
                timeout_s=timeout_s,
                release=release,
                result=result,
            )
        return await self._run_blocking(
            handler=handler,
            payload=payload,
            timeout_s=timeout_s,
            release=release,
            result=result,
        )

    # -- admission ---------------------------------------------------------
    def _admissible_locked(
        self, *, plugin_id: str, is_async: bool, cost: int, scope: ExecutionScope
    ) -> tuple[str, str]:
        """Dry-run admission. Never mutates.

        Separating "would this fit?" from "charge it" is what lets the dispatcher
        inspect a waiter without billing a call it then declines.
        """
        if self._closed:
            return "closed", "the host is shutting down"
        if scope.aborted:
            return "cancelled", "the invocation was cancelled"
        if self._barrier.blocks(plugin_id):
            return "revoked", "the plugin's approval was withdrawn"
        if scope.live >= self.max_concurrent_per_invocation:
            return (
                "capacity",
                f"invocation already has {scope.live} host call(s) in flight "
                f"(limit {self.max_concurrent_per_invocation})",
            )
        outstanding = self._counters.get(plugin_id)
        if outstanding >= self.max_outstanding_per_plugin:
            return (
                "capacity",
                f"plugin already has {outstanding} host call(s) in flight "
                f"(limit {self.max_outstanding_per_plugin})",
            )
        if self._counters.global_count >= self.max_concurrent:
            return (
                "capacity",
                f"host is already running {self._counters.global_count} host call(s) "
                f"(limit {self.max_concurrent})",
            )
        if not is_async and self._counters.blocking >= self.blocking_workers:
            return (
                "capacity",
                f"blocking pool is full at {self._counters.blocking} worker(s) "
                f"(limit {self.blocking_workers})",
            )
        if scope.cost_spent + cost > scope.max_cost:
            return (
                "cost",
                f"cost budget of {scope.max_cost} would be exceeded "
                f"({scope.cost_spent} spent, {cost} requested)",
            )
        return "ok", ""

    def _charge_locked(
        self, *, plugin_id: str, is_async: bool, cost: int, scope: ExecutionScope
    ) -> None:
        """Reserve the slot and bill the call, atomically."""
        scope.cost_spent += cost
        scope.charges += 1
        scope.live += 1
        self._counters.acquire(plugin_id, blocking=not is_async)
        self._started += 1

    def _rate_refusal_locked(
        self, plugin_id: str, capability: str, capability_rate: float | None
    ) -> str | None:
        """Consume one token from the ``(plugin, capability)`` window, or refuse.

        A capability's own rate wins over the host default, so a capability that
        knows it is expensive can be tightened without slowing everything else.
        The window is per plugin *and* per capability: a flood from one plugin
        cannot spend another plugin's allowance, which is the whole reason the
        key is not just the capability name.
        """
        limit_per_s = capability_rate if capability_rate is not None else self.default_rate_per_s
        if limit_per_s is None:
            return None
        allowed = max(1, int(min(float(limit_per_s), MAX_RATE_PER_S) * self.rate_window_s))
        key = (plugin_id, capability)
        bucket = self._rates.get(key)
        if bucket is None:
            bucket = deque()
            self._rates[key] = bucket
        now = self._clock()
        cutoff = now - self.rate_window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= allowed:
            return (
                f"rate limit of {allowed} call(s) per {self.rate_window_s:g}s exceeded "
                f"for {plugin_id}/{capability}"
            )
        bucket.append(now)
        return None

    def _enqueue_locked(
        self,
        *,
        plugin_id: str,
        capability: str,
        cost: int,
        is_async: bool,
        scope: ExecutionScope,
    ) -> _Waiter:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(plugin_id, capability, cost, is_async, scope, loop.create_future())
        self._waiters.setdefault(plugin_id, deque()).append(waiter)
        if plugin_id not in self._rr:
            self._rr.append(plugin_id)
        self._waiter_count += 1
        return waiter

    def _remove_waiter_locked(self, waiter: _Waiter) -> bool:
        """Take ``waiter`` out of the queue. ``False`` means it was already served."""
        queue = self._waiters.get(waiter.plugin_id)
        if queue is None:
            return False
        try:
            queue.remove(waiter)
        except ValueError:
            return False
        self._waiter_count -= 1
        if not queue:
            self._waiters.pop(waiter.plugin_id, None)
            with contextlib.suppress(ValueError):
                self._rr.remove(waiter.plugin_id)
        return True

    async def _await_turn(self, waiter: _Waiter, result) -> ExecutionResult | None:
        """Wait for a grant. Returns a refusal, or ``None`` when admitted.

        ``asyncio.wait`` rather than ``wait_for`` on purpose: ``wait_for`` would
        cancel the future, and a future that is cancelled at the same instant it
        is granted would make the executor charge a slot nobody owns.
        """
        done, _pending = await asyncio.wait({waiter.future}, timeout=self.max_queue_wait_s)
        if done:
            outcome = waiter.future.result()
            return outcome if isinstance(outcome, ExecutionResult) else None

        with self._lock:
            removed = self._remove_waiter_locked(waiter)
        if removed:
            return result(
                CallState.REFUSED,
                reason=(
                    f"waited {self.max_queue_wait_s:.2f}s for an execution slot and none "
                    "freed; refused without executing or charging"
                ),
                refusal=Refusal.QUEUE_TIMEOUT,
                cost=0,
            )
        # ``removed`` is False because the dispatcher took the waiter out of the
        # queue — which means it has charged a slot for us, and will wake us with
        # ``None`` (admitted) or a refusal. It has *already* decided; it has not
        # necessarily *told* us yet: ``_wake`` hops to this loop with
        # ``call_soon_threadsafe``, so the future can still be pending here.
        #
        # Calling ``future.result()`` at this point used to raise
        # ``InvalidStateError``. The exception unwound ``invoke`` *after* the
        # slot was charged and *before* the ``release`` closure existed, so the
        # counter never came back: one leaked slot per race, and once
        # ``max_concurrent`` of them leaked, every host call on the host was
        # refused forever. Wait for the resolution the dispatcher owes us
        # instead of assuming it already happened. It arrives on the next loop
        # iteration; if the loop were closed this coroutine could not be running,
        # so there is nothing to time out against.
        if not waiter.future.done():
            await asyncio.wait({waiter.future})
        outcome = waiter.future.result()
        return outcome if isinstance(outcome, ExecutionResult) else None

    def _dispatch_locked(self) -> list[tuple[asyncio.Future[Any], Any]]:
        """Hand free capacity to waiters in round-robin order.

        Called with the lock held, right after a slot is released or a waiter is
        removed. Returns the futures to wake *after* the lock is dropped, so no
        callback runs under it.

        Round-robin is implemented by moving a served plugin to the back of
        ``_rr``: with A holding three waiters and B one, the order A, B means B
        is served second, not fourth.
        """
        wakes: list[tuple[asyncio.Future[Any], Any]] = []
        while True:
            served = False
            for plugin_id in list(self._rr):
                queue = self._waiters.get(plugin_id)
                if not queue:
                    continue
                waiter = queue[0]
                status, reason = self._admissible_locked(
                    plugin_id=plugin_id,
                    is_async=waiter.is_async,
                    cost=waiter.cost,
                    scope=waiter.scope,
                )
                if status == "capacity":
                    # This plugin's head-of-line call does not fit; let the next
                    # plugin try instead of blocking the whole queue behind it.
                    continue
                if not self._remove_waiter_locked(waiter):
                    continue
                if status == "ok":
                    self._charge_locked(
                        plugin_id=plugin_id,
                        is_async=waiter.is_async,
                        cost=waiter.cost,
                        scope=waiter.scope,
                    )
                    wakes.append((waiter.future, None))
                else:
                    wakes.append((waiter.future, self._refusal_result(waiter, status, reason)))
                self._rotate_locked(plugin_id)
                served = True
                break
            if not served:
                return wakes

    def _rotate_locked(self, plugin_id: str) -> None:
        with contextlib.suppress(ValueError):
            self._rr.remove(plugin_id)
        if self._waiters.get(plugin_id):
            self._rr.append(plugin_id)

    def _refusal_result(self, waiter: _Waiter, status: str, reason: str) -> ExecutionResult:
        return ExecutionResult(
            state=CallState.REFUSED,
            cost=0,
            reason=reason,
            refusal=_REFUSAL_FOR.get(status, Refusal.CONCURRENCY),
        )

    @staticmethod
    def _resolve(future: asyncio.Future[Any], value: Any) -> None:
        if not future.done():
            future.set_result(value)

    def _wake(self, wakes: list[tuple[asyncio.Future[Any], Any]]) -> None:
        """Resolve waiter futures on their own loop, from any thread."""
        for future, value in wakes:
            loop = future.get_loop()
            if loop.is_closed():
                continue
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._resolve, future, value)

    def _refused_locked(self, result, reason: str, why: Refusal) -> ExecutionResult:
        # Caller already holds ``self._lock``; only the counter is touched here.
        self._refused += 1
        return result(CallState.REFUSED, reason=reason, refusal=why, cost=0)

    async def _run_blocking(
        self,
        *,
        handler: Runnable,
        payload: Mapping[str, Any],
        timeout_s: float,
        release,
        result,
    ) -> ExecutionResult:
        """Run ``handler.run`` in a bounded pool; time out without lying.

        The pool is bounded, so "the fix for blocking is more threads" is not
        available here: a saturated pool parks the call in the bounded admission
        queue, and refuses it when that queue is full or its wait expires.
        """
        future = self._pool_or_create().submit(handler.run, dict(payload))

        def on_done(_done) -> None:
            # Consume the exception, if any: nobody will await this future when
            # the caller has already given up, and an unretrieved exception is a
            # warning at best and a silent leak at worst.
            if not _done.cancelled():
                with contextlib.suppress(BaseException):
                    _done.exception()
            release()

        future.add_done_callback(on_done)
        wrapped = asyncio.wrap_future(future)

        done, _pending = await asyncio.wait({wrapped}, timeout=timeout_s)
        if not done:
            # ``cancel()`` returns False for a running thread; the operation
            # continues and keeps its slot until ``on_done`` fires.
            stopped = future.cancel()
            return result(
                CallState.TIMED_OUT,
                reason=(
                    f"host call exceeded {timeout_s:.1f}s; the operation is synchronous "
                    "and cannot be cancelled, so it may still be running"
                ),
                confirmed_stopped=bool(stopped),
            )
        try:
            data = wrapped.result()
        except asyncio.CancelledError:  # pragma: no cover - cancel propagated here
            raise
        except Exception as exc:
            return result(
                CallState.FAILED,
                error=exc,
                reason=f"capability raised {type(exc).__name__}",
            )
        return result(CallState.COMPLETED, data=data, reason="completed")

    async def _run_async(
        self,
        *,
        handler: Runnable,
        payload: Mapping[str, Any],
        scope: ExecutionScope,
        timeout_s: float,
        release,
        result,
    ) -> ExecutionResult:
        """Await a coroutine capability, with a cancellation that is checked.

        ``Task.cancel()`` raises at the coroutine's next await, so an async
        capability usually stops. "Usually" is why the grace wait exists: if it
        does not stop, the state is ``cancellation_requested`` and the audit says
        so.
        """
        task = asyncio.ensure_future(handler.run(dict(payload)))
        self._tasks[task] = scope

        def on_done(_done: asyncio.Task[Any]) -> None:
            self._tasks.pop(_done, None)
            if not _done.cancelled():
                with contextlib.suppress(BaseException):
                    _done.exception()
            release()

        task.add_done_callback(on_done)

        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
        if done:
            if task.cancelled():
                return result(
                    CallState.CANCELLED,
                    reason="capability was cancelled before it finished",
                    confirmed_stopped=True,
                )
            exc = task.exception()
            if exc is not None:
                return result(
                    CallState.FAILED,
                    error=exc,
                    reason=f"capability raised {type(exc).__name__}",
                )
            return result(CallState.COMPLETED, data=task.result(), reason="completed")

        task.cancel()
        stopped, _ = await asyncio.wait({task}, timeout=self.cancellation_grace_s)
        if stopped:
            return result(
                CallState.CANCELLED,
                reason=(
                    f"host call exceeded {timeout_s:.1f}s and the capability confirmed it stopped"
                ),
                confirmed_stopped=True,
            )
        return result(
            CallState.CANCELLATION_REQUESTED,
            reason=(
                f"host call exceeded {timeout_s:.1f}s; cancellation was requested but "
                f"the capability had not stopped after {self.cancellation_grace_s:.2f}s"
            ),
            confirmed_stopped=False,
        )


__all__ = [
    "DEFAULT_CAPABILITY_COST",
    "DEFAULT_MAX_QUEUE_WAIT_S",
    "DEFAULT_MAX_WAITERS",
    "DEFAULT_RATE_WINDOW_S",
    "DEFAULT_SCOPE_COST",
    "MAX_RATE_PER_S",
    "STOPPED_STATES",
    "CallState",
    "DrainReport",
    "ExecutionResult",
    "ExecutionScope",
    "HostExecutor",
    "Refusal",
    "RevocationBarrier",
]
