# Host capability execution

Phase 2.3 answered "may this plugin call this capability". This document answers
the next question: **once it may, what does the host actually do — and does its
report match reality?**

Four facts are kept apart at every layer, because collapsing them is how a
timeout gets recorded as a cancellation:

| Fact | Where it is recorded | What it does **not** mean |
| --- | --- | --- |
| authorised | `HostCallOutcome.decision` (a policy string) | that the call was admitted |
| admitted | `HostCallOutcome.admitted` | that it executed |
| executed | `HostCallOutcome.executed` | that it worked |
| stopped | `HostCallOutcome.confirmed_stopped` | that a timeout achieved it |

For the *motivation* and the rejected alternatives see
[ADR 0015](../decisions/0015-host-capability-execution.md). For the trust
boundary this sits inside, see [wasm-trust-boundary.md](wasm-trust-boundary.md).

## The six states

`CallState` (in `execution.py`) has exactly one value per host call:

| State | Meaning | Can the underlying work still be running? |
| --- | --- | --- |
| `completed` | the capability returned a value | no |
| `failed` | the capability raised | no |
| `timed_out` | the caller's budget expired | **yes — and for a sync capability, certainly** |
| `cancelled` | the operation stopped, confirmed | no |
| `cancellation_requested` | we asked; the grace period expired first | **yes** |
| `refused` | never executed (cost, concurrency, queue, rate, shutdown) | no, nothing started |

`timed_out` is not `cancelled`, and `cancellation_requested` is not `cancelled`
either. A capability that swallows `CancelledError` and keeps working produces
`cancellation_requested`; one that raises it produces `cancelled`. Both are
exercised by tests with capabilities that really behave that way, not by mocks.

## Which capabilities can actually be stopped

| Capability shape | Runs where | Timeout | Cancellation |
| --- | --- | --- | --- |
| `def run(...)` | bounded thread pool (`blocking_workers`, default 4) | yes | **no** — Python has no way to interrupt a running function |
| `async def run(...)` | event loop, as a `Task` | yes | **yes** — `Task.cancel()` raises at the next await |

A capability that claims `supports_cancellation = True` while being a plain `def`
is not believed: the executor inspects `run` and decides. The attribute is
documentation for reviewers and the catalogue, not a switch.

The honest summary: **nothing synchronous is cancellable.** Everything this
document says about cancellation applies only to coroutine capabilities.

## Admission

Four concurrency limits, all host-side, all counted in one place:

| Limit | Default | Counts | Refusal |
| --- | --- | --- | --- |
| per invocation | 2 | calls in flight for one `invoke()` | `-9 CONCURRENCY_LIMIT` |
| per plugin | 4 | calls in flight across a plugin's invocations, **including leftovers** | `-9` |
| global | 8 | calls in flight across the whole host | `-9` |
| blocking pool | 4 | synchronous calls in flight | `-9` |

Order matters, and it is enforced in this sequence:

1. **shutdown** — the host is closing; nothing starts.
2. **cancelled** — the invocation was aborted; nothing starts.
3. **revoked** — this plugin's approval was withdrawn; nothing starts, and nothing
   queued for it will ever be admitted (`decision="revoked"`).
4. **rate** — this plugin is calling this capability too often; nothing starts
   and nothing is charged (`-12 RATE_LIMITED`).
5. **capacity** — a limit above is reached. The call does **not** immediately
   fail; it enters the bounded queue (§Fairness).
6. **cost** — the invocation's budget cannot pay for it; refused (`-8`).
7. **charge**, then **execute**.

Rate is checked before capacity on purpose: a flood should be refused cheaply
instead of occupying queue space a well-behaved plugin could use.

`revoked` is checked here, not only at dispatch, because admission is the last
moment at which "it never started" is still true. `HostExecutor.revoke_plugin()`
drains the queue and sets a per-plugin block under one lock, so a call that was
parked before the decision cannot start after it, and one still on its way in
cannot be admitted either. The block is lifted only by an approval in force
(`allow_plugin`, called from `approve` and from the dispatch guard).

`max_host_calls` (Phase 2.3) is unchanged: it counts *attempts*, including
malformed ones, before decoding. Cost is counted *after* authorisation, on
admission. The two are deliberately different counters with different failure
codes (`-7` vs `-8`).

Admission is also downstream of the decision in a second sense: `dispatch` now
requires an `ALLOW` whose provenance is a grant or a one-shot confirmation, so a
capability allowed only by policy (read-only, by default) is refused as
`policy_default` before any of the counters above are touched; and, when a
lifecycle is wired, an approval in force must cover the capability too
(`not_approved`). Authorisation can only refuse here; admission never widens it.
Phase 2.6, see [plugin-lifecycle.md](plugin-lifecycle.md) §5.

## Fairness: a bounded queue served round-robin

Refuse-when-full is bounded but not fair: one plugin holding every slot keeps
everyone else out forever while staying inside its own quota. Phase 2.5 adds
exactly one mechanism to fix that, and deliberately not a scheduler.

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_waiters` | 16 | total calls that may wait for a slot; `0` disables queueing |
| `max_queue_wait_s` | 0.25 | longest a call may wait before it is refused |

- Only **capacity** refusals are queued. Cost, rate, cancellation and shutdown
  are decided before a queue slot is spent.
- A full queue refuses immediately (`-9`, decision `queue_full`) rather than
  growing: the bound is the point.
- A wait that expires is refused with `-13 QUEUE_TIMEOUT`, executed nothing and
  charged nothing.
- On release, the dispatcher walks plugins in round-robin order and hands the
  freed capacity to the first waiter that fits, moving the served plugin to the
  back. A plugin with three queued calls cannot bury another plugin's first call
  behind all three.
- A waiter is woken **exactly once**, with a grant or a refusal. It never wakes
  to re-race for the slot, which is what makes the order mean something.

**The guarantee is bounded waiting, not strict fairness.** A queued call waits
at most one in-flight operation plus `max_queue_wait_s`. There is no weighting, no
latency SLA, and no fairness across processes. `max_waiters=0` restores Phase
2.4's refuse-fast policy exactly.

## Rate limits

Three counters measure three different things and none substitutes for another:

| Counter | Scope | Unit | Failure |
| --- | --- | --- | --- |
| `max_host_calls` | one invocation | attempts | `-7 BUDGET_EXHAUSTED` |
| `max_cost` | one invocation | declared weight | `-8 COST_EXHAUSTED` |
| `rate_limit_per_s` | one plugin × one capability, over time | calls per window | `-12 RATE_LIMITED` |

The limit comes from the capability (`rate_limit_per_s`) or from
`HostExecutor(default_rate_per_s=...)`; a capability's own value wins, so an
expensive operation can be tightened without slowing the rest. The window is
sliding, keyed by `(plugin_id, capability)`, and per plugin — one plugin's flood
cannot spend another's allowance.

A refused call never executes and never costs anything. It is still counted in
`snapshot()["refused"]` and still appears in the audit, because a refusal the
caller cannot see is a refusal it can ignore.

This is a frequency bound, not a cost model: an adversary can still make each
permitted call expensive. That is what `cost` and the execution timeout are for.


## Cost

```python
class ReferenceMetadataValidate:
    name = "reference.metadata.validate"
    cost = 1  # declared weight, not a measurement
    supports_cancellation = False
```

- The invocation budget is `limits.max_cost` (default 64, ceiling 4096).
- A capability's `cost` defaults to 1, which makes a cost budget behave exactly
  like a call budget until someone declares otherwise.
- Charged **on admission**, in the same critical section that reserves the slot.
  A refused call is not charged. A call that runs and then fails or times out
  **is** charged and is not refunded — charge-then-refund is where double-billing
  lives.
- `InvocationResult.cost_used` / `cost_budget` report the bill, on success *and*
  failure. `ErrorInfo.details` carries both plus the state.

Cost is a weight. It does not claim to predict CPU time: 64 calls to a cheap
validator and 64 calls to a render are only different bills if the render's
capability says so. There is no enforcement that a declared weight is plausible —
that is a review question, and the
[capability catalogue](wasm-capability-catalogue.md) now has a column for it.

## When a slot is released

When the operation **finishes**. Not when the caller stops waiting, and not when
the invocation ends.

That single rule is what makes the limits real. Concretely:

1. A call is admitted → the slot is taken.
2. The caller times out → the slot stays taken, and the plugin's outstanding
   count stays at 1.
3. The next call from that plugin is refused (`-9`) if the per-plugin limit is
   reached.
4. The blocking function returns → the completion callback releases the slot.

Measured: a 500 ms capability under a 50 ms budget leaves `outstanding() == 1`
when `invoke()` returns, and the next call is refused. The mutation
`slot-release-on-timeout` deletes that behaviour and a test goes red.

## Shutdown

`WasmPluginHost.aclose()` stops accepting work, refuses every queued waiter with
a shutdown refusal, cancels the *cancellable* tasks, shuts the pool down
**without waiting**, and returns the number of operations still running.

```python
leftover = await host.aclose()  # e.g. 1
```

Every caller gets an answer: running operations are reported in the return value,
queued ones receive `refused`/`shutdown` on their own call, and finished ones
already returned. Waiting for the pool would hang on exactly the call the host
already gave up on. Reporting the number turns "we shut down" into "we shut down
and left one operation running", which is a fact an operator can act on. It is
not a clean stop, and the method does not pretend to be one.

## What the audit carries

Every dispatch returns a `HostCallOutcome` with `capability`, `status`,
`decision`, `reason`, `duration_ms`, `state`, `admitted`, `executed`, `cost`,
`confirmed_stopped`. `InvocationResult.host_calls` is the ordered trail,
including refusals, timeouts and budget markers.

Two rules that keep the trail readable:

- `decision="allow"` plus `state="timed_out"` is a valid, meaningful pair: it
  means the call was allowed and then did not finish. Neither field is a synonym
  for success.
- `state` is required, and it is always one of `CallState`. It used to default to
  the empty string, which made "the host refused this" and "nobody recorded how
  this ended" the same value. Two paths filled it in late: the budget marker and
  every refusal that happens before dispatch (`reject`). Both now say `refused`,
  because neither ever reached the executor.
- Nothing in a `reason` is interpolated from host paths, tokens or capability
  internals. Reasons are built from fixed strings, sizes, limit values and
  exception *type names*.

## Still not true

- A synchronous capability cannot be interrupted. If it takes 30 seconds, the
  host waits 30 seconds' worth of thread. The only fix is a process and a kill —
  evaluated in [ADR 0016](../decisions/0016-plugin-provenance-and-fairness.md) and
  not implemented.
- Fairness is **bounded, not strict**: round-robin among waiters, no weighting,
  no latency guarantee.
- Every counter, queue and rate window is per executor, i.e. per process. Two
  hosts do not see each other's load.
- The queue is small by design (16). A burst larger than that is refused, not
  absorbed; that is the intended trade, not a limitation to be fixed by growing
  the number.
