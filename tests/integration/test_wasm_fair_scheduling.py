"""Fair scheduling: does one plugin's flood stop another plugin from ever running?

Phase 2.4 refused when full, which is bounded but not fair: a plugin holding
every slot refuses everyone else forever while staying inside its own quota.
Phase 2.5 adds a bounded queue served round-robin. These tests measure that
claim with real blocking/async capabilities — never a mock asserting a method
was called, because the properties are about wall-clock order.

What is *not* claimed: strict fairness or a latency guarantee. A queued call
waits at most one in-flight operation plus ``max_queue_wait_s``; under
continuous saturation every waiter eventually gets a turn, in round-robin order.
"""

from __future__ import annotations

import asyncio

import pytest

from deskweave_plugin_wasm import (
    CallState,
    ExecutionScope,
    HostExecutor,
    Refusal,
)
from support import host_capabilities as caps


def _scope(plugin_id: str, *, max_cost: int = 64) -> ExecutionScope:
    return ExecutionScope(plugin_id, max_cost=max_cost)


async def _run(
    executor: HostExecutor,
    capability,
    *,
    plugin_id: str = "A",
    scope: ExecutionScope | None = None,
    timeout_s: float = 2.0,
):
    return await executor.run(
        plugin_id=plugin_id,
        capability=capability.name,
        handler=capability,
        payload={},
        scope=scope or _scope(plugin_id),
        timeout_s=timeout_s,
    )


# -- 1. the point of the queue: B is not buried behind A's backlog -------------


async def test_a_second_plugin_is_served_before_the_first_plugins_backlog() -> None:
    """With one slot and four queued calls, B must not wait for all of A's work.

    Arrival order is A1, A2, A3, A4, then B. Strict FIFO would finish B last;
    round-robin finishes it third, after only one of A's queued calls.
    """
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=8,
        max_outstanding_per_plugin=8,
        max_waiters=8,
        max_queue_wait_s=2.0,
    )
    slow = caps.BlockingCapability(name="test.slow", delay=0.12)
    fast = caps.BlockingCapability(name="test.fast", delay=0.0)

    order: list[str] = []

    async def call(plugin_id: str, capability) -> None:
        await _run(executor, capability, plugin_id=plugin_id)
        order.append(plugin_id)

    first = asyncio.create_task(call("A", slow))
    await asyncio.sleep(0.03)  # let A1 take the only slot
    rest = [asyncio.create_task(call("A", slow)) for _ in range(3)]
    second_plugin = asyncio.create_task(call("B", fast))

    await asyncio.gather(first, *rest, second_plugin)

    assert order[0] == "A"
    assert order[2] == "B", f"B waited behind A's whole backlog: {order}"
    assert len(order) == 5
    await executor.aclose()


async def test_the_queue_is_bounded_across_plugins() -> None:
    """A full queue refuses even a different plugin: the bound is global on purpose."""
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=2,
        max_queue_wait_s=1.0,
    )
    slow = caps.BlockingCapability(name="test.slow", delay=0.3)

    running = asyncio.create_task(_run(executor, slow, plugin_id="A"))
    await asyncio.sleep(0.03)
    waiting = [
        asyncio.create_task(_run(executor, slow, plugin_id="A")),
        asyncio.create_task(_run(executor, slow, plugin_id="B")),
    ]
    await asyncio.sleep(0.03)
    assert executor.queued() == 2

    refused = await _run(executor, slow, plugin_id="C")

    assert refused.state is CallState.REFUSED
    assert refused.refusal is Refusal.QUEUE_FULL
    assert refused.executed is False
    await asyncio.gather(running, *waiting)
    await executor.aclose()


# -- 2. quotas cannot be bypassed by queueing ---------------------------------


async def test_a_plugin_cannot_exceed_its_own_concurrency_by_queueing() -> None:
    """Ten queued calls still run one at a time when the plugin limit is one."""
    executor = HostExecutor(
        max_concurrent=4,
        blocking_workers=4,
        max_outstanding_per_plugin=1,
        max_concurrent_per_invocation=8,
        max_waiters=16,
        max_queue_wait_s=5.0,
    )
    cap = caps.BlockingCapability(name="test.serial", delay=0.02)

    results = await asyncio.gather(*(_run(executor, cap, plugin_id="A") for _ in range(8)))

    assert all(result.state is CallState.COMPLETED for result in results)
    assert cap.runs == 8
    assert cap.max_running == 1, "the per-plugin limit was bypassed"
    await executor.aclose()


# -- 3. a timeout does not hand the slot to the queue early -------------------


async def test_a_timed_out_operation_keeps_its_slot_until_it_really_stops() -> None:
    """The queued caller must wait for the *thread*, not for the caller's patience."""
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=4,
        max_queue_wait_s=2.0,
    )
    cap = caps.BlockingCapability(name="test.slow", delay=0.3)

    timed_out = await _run(executor, cap, plugin_id="A", timeout_s=0.05)
    assert timed_out.state is CallState.TIMED_OUT
    assert timed_out.confirmed_stopped is False
    assert executor.outstanding("A") == 1

    second = asyncio.create_task(_run(executor, cap, plugin_id="B"))
    await asyncio.sleep(0.1)
    assert cap.runs == 1, "the queued call started while the thread was still running"
    assert executor.queued() == 1

    result = await second
    assert result.state is CallState.COMPLETED
    assert cap.runs == 2
    await executor.aclose()


async def test_a_queue_timeout_refuses_without_executing_or_charging() -> None:
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=4,
        max_queue_wait_s=0.05,
    )
    cap = caps.BlockingCapability(name="test.slow", delay=0.3)
    scope = _scope("B")

    running = asyncio.create_task(_run(executor, cap, plugin_id="A"))
    await asyncio.sleep(0.02)

    refused = await _run(executor, cap, plugin_id="B", scope=scope)

    assert refused.state is CallState.REFUSED
    assert refused.refusal is Refusal.QUEUE_TIMEOUT
    assert refused.cost == 0
    assert scope.cost_spent == 0, "a refused call was charged"
    assert cap.runs == 1
    await running
    await executor.aclose()


# -- 4. rate limits -----------------------------------------------------------


async def test_a_rate_limit_refuses_before_execution_and_charges_nothing() -> None:
    executor = HostExecutor(max_concurrent=4, blocking_workers=4, max_waiters=0, rate_window_s=1.0)
    cap = caps.BlockingCapability(name="test.rated", delay=0.0, rate_limit_per_s=2)

    results = [await _run(executor, cap, plugin_id="A") for _ in range(3)]

    assert [result.state for result in results] == [
        CallState.COMPLETED,
        CallState.COMPLETED,
        CallState.REFUSED,
    ]
    assert results[2].refusal is Refusal.RATE
    assert cap.runs == 2, "the third call reached the capability"
    assert results[2].cost == 0
    await executor.aclose()


async def test_one_plugins_rate_limit_does_not_slow_another() -> None:
    """The window is keyed by (plugin, capability), so B has its own allowance."""
    executor = HostExecutor(max_concurrent=4, blocking_workers=4, max_waiters=0, rate_window_s=1.0)
    cap = caps.BlockingCapability(name="test.rated", delay=0.0, rate_limit_per_s=1)

    first = await _run(executor, cap, plugin_id="A")
    refused = await _run(executor, cap, plugin_id="A")
    other = await _run(executor, cap, plugin_id="B")

    assert first.state is CallState.COMPLETED
    assert refused.refusal is Refusal.RATE
    assert other.state is CallState.COMPLETED, "one plugin's flood blocked another"
    assert cap.runs == 2
    await executor.aclose()


async def test_a_default_rate_applies_when_a_capability_declares_none() -> None:
    executor = HostExecutor(
        max_concurrent=4,
        blocking_workers=4,
        max_waiters=0,
        default_rate_per_s=1,
        rate_window_s=1.0,
    )
    cap = caps.BlockingCapability(name="test.unrated", delay=0.0)

    assert (await _run(executor, cap, plugin_id="A")).state is CallState.COMPLETED
    assert (await _run(executor, cap, plugin_id="A")).refusal is Refusal.RATE
    await executor.aclose()


# -- 5. cancellation, failure and shutdown leave the scheduler usable ---------


async def test_cancelling_an_invocation_refuses_its_queued_calls() -> None:
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=4,
        max_queue_wait_s=2.0,
    )
    cap = caps.BlockingCapability(name="test.slow", delay=0.25)
    doomed = _scope("B")

    running = asyncio.create_task(_run(executor, cap, plugin_id="A"))
    await asyncio.sleep(0.02)
    queued = asyncio.create_task(_run(executor, cap, plugin_id="B", scope=doomed))
    await asyncio.sleep(0.02)
    assert executor.queued() == 1

    executor.abort_scope(doomed)
    result = await queued

    assert result.state is CallState.REFUSED
    assert result.refusal is Refusal.CANCELLED
    assert result.executed is False
    assert executor.queued() == 0
    await running
    await executor.aclose()


async def test_a_failure_does_not_corrupt_the_queue_or_the_counters() -> None:
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=4,
        max_queue_wait_s=2.0,
    )
    slow = caps.BlockingCapability(name="test.slow", delay=0.05)
    boom = caps.RaisingCapability()
    boom.name = "test.raises"

    running = asyncio.create_task(_run(executor, slow, plugin_id="A"))
    await asyncio.sleep(0.01)
    failing = asyncio.create_task(_run(executor, boom, plugin_id="B"))
    following = asyncio.create_task(_run(executor, slow, plugin_id="C"))

    await running
    failed = await failing
    ok = await following

    assert failed.state is CallState.FAILED
    assert ok.state is CallState.COMPLETED
    assert executor.queued() == 0
    assert executor.outstanding() == 0
    await executor.aclose()


async def test_shutdown_refuses_queued_calls_instead_of_leaving_them_waiting() -> None:
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=4,
        max_outstanding_per_plugin=4,
        max_waiters=4,
        max_queue_wait_s=5.0,
    )
    cap = caps.BlockingCapability(name="test.slow", delay=0.25)

    running = asyncio.create_task(_run(executor, cap, plugin_id="A"))
    await asyncio.sleep(0.02)
    queued = asyncio.create_task(_run(executor, cap, plugin_id="B"))
    await asyncio.sleep(0.02)

    outstanding = await executor.aclose()

    result = await queued
    assert result.state is CallState.REFUSED
    assert result.refusal is Refusal.SHUTDOWN
    assert outstanding >= 1, "the running blocking call was reported as gone"

    await running
    assert cap.runs == 1


# -- 6. budgets stay per invocation -------------------------------------------


async def test_cost_budgets_are_scoped_to_one_invocation() -> None:
    """One scope spends its budget once; a new invocation of the same plugin starts fresh."""
    executor = HostExecutor(max_concurrent=2, blocking_workers=2, max_waiters=0)
    cap = caps.BlockingCapability(name="test.costly", delay=0.0, cost=2)
    shared = _scope("A", max_cost=2)

    first = await _run(executor, cap, plugin_id="A", scope=shared)
    exhausted = await _run(executor, cap, plugin_id="A", scope=shared)
    fresh = await _run(executor, cap, plugin_id="A", scope=_scope("A", max_cost=2))

    assert first.state is CallState.COMPLETED
    assert exhausted.state is CallState.REFUSED
    assert exhausted.refusal is Refusal.COST
    assert fresh.state is CallState.COMPLETED, "the budget leaked into the next invocation"
    await executor.aclose()


def test_a_zero_wait_queue_is_a_configuration_error() -> None:
    """A queue that can never wait is a typo, not a policy; refuse it at construction."""
    with pytest.raises(ValueError, match="max_queue_wait_s"):
        HostExecutor(max_waiters=4, max_queue_wait_s=0)


def test_a_negative_rate_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="default_rate_per_s"):
        HostExecutor(default_rate_per_s=-1)


async def test_queueing_does_not_delay_the_event_loop() -> None:
    """A queued call awaits a future; it must not spin or block the loop."""
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_concurrent_per_invocation=2,
        max_outstanding_per_plugin=2,
        max_waiters=2,
        max_queue_wait_s=1.0,
    )
    cap = caps.BlockingCapability(name="test.slow", delay=0.15)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    running = asyncio.create_task(_run(executor, cap, plugin_id="A"))
    await asyncio.sleep(0.02)
    queued = asyncio.create_task(_run(executor, cap, plugin_id="B"))
    await asyncio.gather(running, queued)
    beat.cancel()

    assert ticks >= 15, f"the loop starved while a call was queued ({ticks} ticks)"
    await executor.aclose()


# -- 6. the deadline and the wake can cross --------------------------------


async def test_a_wake_that_lands_after_the_deadline_still_admits_the_call() -> None:
    """The dispatcher owns a waiter the instant it leaves the queue.

    ``_await_turn`` gives up by removing its waiter under the lock. The
    dispatcher can get there first: it takes the same waiter out of the queue,
    charges a slot, and only *then* hops back to this loop through
    ``call_soon_threadsafe``. The future is still pending at that moment, so
    reading it with ``future.result()`` raised ``InvalidStateError`` — after the
    slot had been charged and before the caller owned the ``release`` closure
    that would have returned it. One leaked slot per race, and once
    ``max_concurrent`` slots had leaked the host refused every call, for every
    plugin, for the rest of its life.

    White-box on purpose: the interleaving is a few microseconds wide and cannot
    be produced reliably by racing real calls. So the dispatcher is invoked for
    real, its wake is delayed past the deadline for real, and the only thing
    simulated is *when* the callback runs.
    """
    executor = HostExecutor(
        max_concurrent=1,
        blocking_workers=1,
        max_waiters=4,
        max_queue_wait_s=0.05,
    )
    scope = _scope("A")

    with executor._lock:
        waiter = executor._enqueue_locked(
            plugin_id="A", capability="test.gated", cost=1, is_async=True, scope=scope
        )
        wakes = executor._dispatch_locked()

    assert [future for future, _ in wakes] == [waiter.future]
    assert not waiter.future.done(), "the wake is scheduled, not yet delivered"

    def must_not_refuse(*_args, **_kwargs):
        raise AssertionError("the dispatcher owns the slot; this call was not refused")

    # Deliver the real wake after the real deadline: 50 ms of waiting, 120 ms
    # before the dispatcher's answer arrives.
    asyncio.get_running_loop().call_later(0.12, executor._wake, wakes)

    outcome = await executor._await_turn(waiter, must_not_refuse)

    assert outcome is None, "admitted: the caller owns the slot that was charged"
    assert executor._counters.global_count == 1, "exactly one slot is held, not zero and not two"

    with executor._lock:
        executor._counters.release("A", blocking=False)
    await executor.aclose()
