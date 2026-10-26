# ADR 0015 — Host capabilities are governed, not merely authorised

- Status: accepted (prototype, not wired into `Runtime`)
- Date: 2026-10-23
- Phase: 2.4
- Supersedes nothing; extends [ADR 0014](0014-wasm-plugin-isolation.md)

## Context

Phase 2.3 closed the authorisation question: a plugin cannot call a capability it
was not granted, and cannot hide a refusal. Its own verification left three
findings that were recorded rather than fixed, because fixing them was a larger
change than that phase covered:

1. `HostCapability.run()` ran on the event loop thread. A 250 ms capability froze
   every other coroutine for 250 ms — measured, not theorised.
2. `future.cancel()` stopped the *caller* waiting; it never stopped the work. With
   a 50 ms budget a 250 ms capability still ran to completion, and the invocation
   was reported as a timeout while the browser/render/whatever kept going.
3. `max_host_calls` counted calls. A cheap call and an expensive call cost the
   same, so 64 "validate some metadata" calls and 64 "launch a browser" calls had
   the same budget. The count was not a cost.

Phase 2.4 answers a narrower question than "build a scheduler": **what must be
true before a host capability may be exposed to third-party code at all?**

## Options considered

| Option | Why not |
| --- | --- |
| A. Keep running on the loop, wrap in `asyncio.wait_for` | The bug. `wait_for` cancels the *waiter*, not the thread; the loop stays frozen for the whole call |
| B. `asyncio.to_thread` for every capability, unbounded | Moves the blocking off the loop and introduces an unbounded thread factory. A plugin that times out 1 000 calls leaves 1 000 threads. This is "fixing" a resource bug by removing the resource limit |
| C. **Bounded pool + explicit admission + a four-state result** | Chosen. Every limit is a counter you can read, every refusal is deterministic, and the audit can distinguish "we stopped waiting" from "it stopped" |
| D. A process per capability | Real isolation and real cancellation (kill), but: an IPC protocol for every call, a platform-specific sandbox per OS, and JSON serialisation on both sides for capabilities that are currently microseconds of arithmetic. Revisit if a capability ever needs to survive `kill` cleanly |
| E. Require every capability to be `async def` | Does not work. `async def` around a blocking call still blocks; `async def` around a subprocess needs `D`'s machinery anyway. Worse, it would migrate existing synchronous capabilities for no gain |

## Decisions

1. **Four states, not two.** `completed`, `timed_out`, `cancelled`,
   `cancellation_requested`. `timed_out` never implies the operation stopped;
   `cancelled` is only used when the coroutine actually finished or raised
   `CancelledError`; `cancellation_requested` is what you get when the grace
   period expires first. A single boolean could not express this, which is how
   the old code ended up reporting a timeout as containment.
2. **The executor decides cancellability by inspecting `run`, not by trusting a
   flag.** A plain `def` runs in the bounded pool (timeout only). An `async def`
   runs as a task on the loop (cancellable). A `supports_cancellation = True`
   attribute that lied would otherwise become a false audit claim.
3. **Admission is refuse-when-full, never queue.** Four limits: per invocation,
   per plugin, globally, and blocking-pool capacity. Queueing converts a limit
   into latency and then into memory; a refusal is immediate, countable and
   auditable.
4. **A slot is released when the operation finishes, not when the caller gives
   up.** This is the single most important invariant in the module. Decrementing
   on timeout would make every limit above advisory.
5. **Cost is charged on admission, in the same critical section that admits the
   call, and never refunded.** Charge-then-refund is where double-billing bugs
   live. A call that is refused is not charged; a call that runs and fails is.
6. **Cost is a declared weight, not a measurement.** Each capability states
   `cost`; the budget is `limits.max_cost`. This phase deliberately does not claim
   to model real CPU time — a number that is honest about being a weight is more
   useful than one that pretends to be a profile.
7. **One executor per host, shared by every plugin it loads.** A global limit
   counted in two places is not a limit.
8. **Shutdown does not wait for work it cannot stop.** `aclose()` returns the
   number of operations still running. `shutdown(wait=True)` on a pool holding an
   uninterruptible call is a hang with extra steps.
9. **The error taxonomy does not change.** New failure modes map onto the
   existing `PluginResourceError` / `PluginHostError` classes, with the state
   carried in `ErrorInfo.details`. Adding error codes would be a public contract
   break for a distinction that already exists in the audit record.

## Consequences

- The event loop stays responsive during a blocking capability. Proven by a
  heartbeat test, not by inspection.
- An `async` capability can be genuinely stopped; a `sync` one cannot, and the
  host says so in the audit rather than in a comment.
- A plugin can be refused for reasons it cannot influence (host saturation), and
  the refusal is charged to nothing.
- Cost budgets only work if capability authors declare weights honestly. There is
  no enforcement of "is this number plausible" — that is a review question, and
  the catalogue now has a column for it.
- Four counters and a thread pool are more moving parts than before. The
  mitigation is that all four are readable in one `snapshot()` and every one of
  them is asserted by a test that fails when its increment moves.

## Limits — what this does **not** provide

- **No preemption.** A blocking capability runs to completion. The host can stop
  *waiting*, not stop *working*. Anything that needs real preemption needs a
  process and a kill, which is option D.
- **No cost prediction.** `cost` is static per capability. A capability whose
  cost depends on its input (render 2 frames vs 2 000) cannot express that yet.
- **No admission across hosts.** Two `WasmPluginHost` instances with separate
  executors have separate global limits. Sharing one executor is possible and is
  the caller's decision.
- **No fairness.** Refusal is first-come; a busy plugin can keep another plugin
  out. Rate limiting and per-plugin share are not implemented.

  *Superseded in part by [ADR 0016](0016-plugin-provenance-and-fairness.md)
  Decision 2 (Phase 2.5): refusal is no longer first-come — a full host parks up to
  16 waiters for at most 0.25 s and serves plugins round-robin, and capabilities
  may declare `rate_limit_per_s`. The claim remains bounded waiting, not
  fairness: no weighting, no latency guarantee, per executor instance.*
- **Queue depth is zero by construction**, so a burst is refused rather than
  absorbed. That is a deliberate trade of throughput for predictability.

  *Superseded by ADR 0016 Decision 2: depth is now 16 by default and
  `max_waiters=0` restores the zero-depth behaviour exactly.*

## Tests

`tests/integration/test_wasm_host_execution.py` — 14 cases covering the four
states, admission refusal, cost accounting, slot lifetime, invocation
cancellation in both directions, and shutdown. Real capabilities from
`tests/support/host_capabilities.py`: one that blocks a thread, one that awaits,
one that catches `CancelledError` and keeps going, one that raises.

Five of the guard mutations in `scripts/wasm_guard_mutation_check.py`
(`event-loop-offload`, `timeout-is-not-cancellation`, `slot-release-on-timeout`,
`cost-check-before-execution`, `cancellation-confirmation`) delete each control in
turn and require a red test. Removing any of them without a failure would mean the
corresponding claim above is unearned.

Phase 2.5 added four more against the admission and rate paths
(`queue-fairness`, `queue-bound`, `rate-limit`, `queue-refusal-not-charged`) and
`tests/integration/test_wasm_fair_scheduling.py` (15 cases) to drive them. Those
tests use a real bounded-capacity host and a genuinely blocking capability, and
they assert *ordering* (`B` is served 3rd of 5, not last) rather than that a mock
was called — a mock would be satisfied by a queue that never rotates.
