# The WASM plugin runtime

`packages/plugin-wasm` loads and runs a third-party plugin in a WebAssembly
sandbox and gives it exactly one way to affect the world: a host call that is
authorised by the same permission layer that guards browser and Blender calls.

This document describes the call chain, the byte-level contract and the failure
taxonomy. For *why* this design was chosen over the Component Model, see
[ADR 0014](../decisions/0014-wasm-plugin-isolation.md). For what it does not
protect against, see the [threat model](wasm-threat-model.md). For the
layer-by-layer view of where untrusted input crosses into trusted code, and
which layer enforces what, see the
[trust boundary map](wasm-trust-boundary.md). For what "this file is the file
that was installed" does and does not buy, see
[plugin installation](plugin-installation.md).

## Where it sits

```
   plugin.wasm  (any language -> wasm32-wasip1)
        |
        |  path validated lexically, then hashed, then parsed — once
        |  activation_guard: does a current approval cover *this* build?  -> plugin_not_approved
        v
   WasmPluginHost            packages/plugin-wasm/src/.../runtime.py
        |  Store per call, Engine per plugin
        |  fuel + epoch + memory cap
        v
   HostCapabilityRegistry    .../host.py
        |  exists? declared? schema? authorised?
        v
   PermissionManager         runtime/src/deskweave_runtime/permissions
        |  policy: one-shot, explicit grant, risk class
        v
   HostExecutor              .../execution.py
        |  rate? cost? concurrency? charge, reserve a slot, then run
        |  sync -> bounded pool   async -> task + grace period
        v
   HostCapability.run()      pure host-side work (no FS, no network, no Blender)
```

Nothing below `PermissionManager` knows a plugin exists. Nothing above
`WasmPluginHost` knows Wasmtime exists. `Runtime`, the MCP gateway, the task state
machine and the adapters are untouched — the package is a library that takes a
`PermissionChecker`, which is why it could be built without editing any of them.

The approval row in that diagram is opt-in and applied by the caller:
`WasmPluginHost(activation_guard=...)`. `PluginLifecycle` always supplies one, so
a plugin it loads has been hashed **and** approved; a bare `WasmPluginHost()`
supplies none and has no notion of approval. That asymmetry is deliberate — the
negative and pre-authorisation tests need an unguarded host — and it is the reason
"the approval boundary" is a property of the lifecycle, not of the package. See
[plugin-lifecycle.md](plugin-lifecycle.md).

Since Phase 2.6.4 the asymmetry has a second floor, and it is the one people get
wrong: **loading is not authorising.** A host that *has* a guard now refuses to
load unless the caller also passes the approval snapshot that guard is asked to
judge — `load(module, manifest_path, manifest=..., approval=...)`. Without it the
host would answer "may this run?" from whatever it last read, and a build revoked
by another lifecycle since then would still be instantiated (Phase 2.6.4 F2,
measured). The snapshot can only come from refreshing the approval store, which
`PluginLifecycle.activate()` does under its lock, so the snapshot is a fact about
the present rather than a cache with a timestamp.

Since Phase 2.6.2 the asymmetry has a floor: a host that pins bytes
(`require_install_record=True`) but supplies no guard raises `plugin_lifecycle`
at construction, and `allow_unguarded=True` is the explicit way to say "this host
checks bytes and nothing else". The dangerous configuration is no longer reachable
by forgetting a keyword.

## The ABI

A plugin is a **core** WebAssembly module (`wasm32-wasip1`). It must export five
names and may import only one.

| Exports | Signature | Purpose |
| --- | --- | --- |
| `memory` | linear memory, min 1 page | everything is read and written here |
| `dw_abi_version` | `() -> i32` | must equal `ABI_VERSION` (currently `1`) |
| `dw_alloc` | `(len: i32) -> i32` | return a pointer the host may write `len` bytes to; `0` on failure |
| `dw_free` | `(ptr: i32, len: i32) -> ()` | release a buffer; best effort, never trusted |
| `dw_invoke` | `(ptr: i32, len: i32) -> i32` | read the request frame at `ptr`, return a pointer to a response frame |

| Imports | Signature | Purpose |
| --- | --- | --- |
| `env.dw_host_call` | `(req_ptr, req_len, out_ptr, out_cap: i32) -> i32` | one authorised host call; positive = bytes written, negative = `HostCallStatus` |

Anything else an import section asks for fails instantiation. There is no
`wasi_snapshot_preview1` file, socket or process function to reach, because WASI
is configured with no preopened directories and an empty environment.

### Framing

Every payload, in both directions, is

```
+--------------------------+--------------------------------+
| u32 little-endian length |  length bytes of UTF-8 JSON    |
+--------------------------+--------------------------------+
```

Two consequences that matter:

- **The host reads the 4-byte header first**, then only the declared number of
  bytes, clamped to `max_output_bytes`. A plugin that claims a 64 MiB response
  gets `plugin_invalid_output` and the host never allocated anything.
- **The plugin owns the host-call output buffer.** `dw_host_call` writes into
  `out_ptr` only if the response fits in `out_cap`, otherwise returns
  `BUFFER_TOO_SMALL` (-5). The host never allocates inside the sandbox, so a
  greedy plugin cannot make the host a memory allocator on its behalf.

### Host-call status codes

| Code | Meaning |
| --- | --- |
| `>= 0` | success; the value is the number of bytes written to `out_ptr` |
| `-1` `DENIED_NOT_DECLARED` | the manifest never mentioned this capability |
| `-2` `DENIED_NO_GRANT` | declared, but no grant or confirmation covers the call |
| `-3` `DENIED_UNKNOWN_CAPABILITY` | this build does not offer the capability at all |
| `-4` `INVALID_ARGUMENT` | payload failed the capability's JSON Schema |
| `-5` `BUFFER_TOO_SMALL` | the response does not fit in `out_cap` |
| `-6` `HOST_ERROR` | the capability raised, or the host-side call failed or timed out; the plugin gets a reason, not a traceback |
| `-7` `BUDGET_EXHAUSTED` | this invocation has already used `limits.max_host_calls` host calls |
| `-8` `COST_EXHAUSTED` | this invocation's cost budget cannot cover another call at this capability's weight |
| `-9` `CONCURRENCY_LIMIT` | admission refused: too many host calls in flight (invocation / plugin / host / blocking pool) and no room in the admission queue |
| `-10` `CANCELLED` | the operation stopped and that was confirmed |
| `-11` `CANCELLATION_UNCONFIRMED` | cancellation was requested; the operation had not stopped by the grace deadline |
| `-12` `RATE_LIMITED` | this plugin already made too many calls to this capability inside the rate window |
| `-13` `QUEUE_TIMEOUT` | the call waited `max_queue_wait_s` for a slot and never got one |

`BUDGET_EXHAUSTED` is deliberately a separate code from the denials. Nothing was
refused by policy — the plugin simply asked too many times — so the invocation
must fail as a *resource* problem (`plugin_resource_exhausted`) rather than as an
authorisation problem. Merging it into `DENIED_NO_GRANT` would have made a
loop-happy plugin look like a victim of a bad policy.

`-12` and `-13` follow the same logic and are separate from `-9` on purpose. All
three are refusals that happen *before* the capability runs, but they say
different things to whoever is reading the log: `-9` means the host is saturated,
`-13` means this plugin waited its turn and the queue was too long, `-12` means
this plugin in particular is calling too fast. Collapsing them would make "we are
overloaded" indistinguishable from "you are abusing us", and the operator's
response to each is different. All three are recorded with a `decision` field
(`concurrency`, `queue_full`, `queue_timeout`, `rate`) and none of them execute
or charge anything.

Every code is also recorded in `InvocationResult.host_calls`, so the host's own
audit survives a plugin that ignores the number it was given. Refusals are
recorded through `_HostBridge.reject()` *before* the status is returned, which is
what makes "swallow the denial and claim success" impossible.

The budget counts **attempts**, not successful dispatches. A plugin that gets its
request rejected by the schema has still spent a call, otherwise malformed
requests would be a free denial of service. The record for hitting the budget is
written once per invocation, not once per blocked attempt — otherwise a 10 000
call loop would fill the audit log with the same line.

## The call chain, end to end

```
host.invoke(plugin, payload)
  |
  +-- validate payload against manifest.input_schema     -> plugin_invalid_input
  +-- asyncio.to_thread(_run, ...)          one worker thread, one Store
        |
        +-- _instantiate():  Engine -> Linker -> Store
        |     set_fuel(limits.fuel)
        |     set_limits(memory_size=limits.max_memory_bytes)
        |     set_epoch_deadline(1)          <- see "Pitfalls"
        |     WASI: no preopens, empty env, callback sinks
        |     check exports AND signatures   -> plugin_malformed
        |     read dw_abi_version            -> plugin_abi_mismatch
        |
        +-- dw_alloc(len(payload)); _guest_write() bounds-checked
        +-- threading.Timer(timeout_ms) -> engine.increment_epoch()
        +-- dw_invoke(ptr, len)              -> trap? plugin_timeout / _trapped / _resource
        |     |
        |     +-- (inside the guest) env.dw_host_call
        |           -> _HostBridge.enter()      budget? -> -7 BUDGET_EXHAUSTED
        |           -> read request frame       bad ptr/len? -> -4, recorded
        |           -> run_coroutine_threadsafe(registry.dispatch(...), loop)
        |              -> dispatch: unknown? declared? schema? authorised?
        |                 authorised = ALLOW **and** via in {GRANT, ONE_SHOT}
        |                 policy-default ALLOW? -> -6, decision "policy_default"
        |              -> HostExecutor.run()    rate?  -> -12 (before anything runs)
        |                                       cost?  -> -8
        |                                       full?  -> queue (bounded) or -9 / -13
        |                 sync  -> bounded pool  -> timed_out (cannot stop)
        |                 async -> task + grace  -> cancelled / cancellation_requested
        |           -> _guest_write(response)   no room? -> -5
        |
        +-- _read_frame(): 4-byte header, then body, clamped
        +-- validate against manifest.output_schema   -> plugin_invalid_output
        +-- fuel_used = limits.fuel - store.get_fuel()
  |
  +-- any recorded denial or exhausted budget outranks the result
  +-- containment net: an unexpected host exception becomes plugin_trapped
  +-- InvocationResult(ok, data, error, duration_ms, fuel_used, host_calls, stdout)
```

The blocking half runs on a worker thread because Wasmtime execution is
synchronous and a plugin that spins must not block the event loop. The host call
goes the other way — back onto the event loop — because the permission layer is
`async`. `asyncio.run_coroutine_threadsafe` plus a bounded `future.result()` is
the bridge, and its timeout is why a capability that blocks cannot wedge a
plugin forever.

## Failure taxonomy

`invoke()` does not raise. Every path either returns a result or raises a
`PluginError`, which `invoke()` converts to `ok=False` plus `ErrorInfo`:

| Error code | Raised when |
| --- | --- |
| `plugin_not_found` | the module file does not exist |
| `plugin_malformed` | manifest missing/invalid, module unloadable, an export missing or carrying the wrong signature, a trap during instantiation, or instantiation refused (including "minimum memory exceeds limit") |
| `plugin_abi_mismatch` | `interface_version` in the manifest, or `dw_abi_version` in the module, differs from the host |
| `plugin_invalid_input` | payload fails `input_schema`, or exceeds `max_input_bytes` |
| `plugin_invalid_output` | response is not a JSON object, does not match `output_schema`, exceeds `max_output_bytes`, or lies about its length |
| `plugin_timeout` | the epoch watchdog fired, or an `INTERRUPT` trap arrived |
| `plugin_trapped` | `unreachable`, out-of-bounds access, `proc_exit`, or any other trap |
| `plugin_resource_exhausted` | fuel exhausted (`OUT_OF_FUEL`), host calls exhausted (`-7`), cost budget exhausted (`-8`), admission refused (`-9`, `-13`), or rate limited (`-12`) |
| `plugin_capability_undeclared` | a host call named a capability the manifest never declared |
| `plugin_capability_denied` | declared, but the permission layer refused |
| `plugin_host_error` | the host side of an accepted call failed: the capability raised, the call timed out, or it was cancelled (`-10` / `-11`) |

`-8`, `-9`, `-12` and `-13` deliberately reuse `plugin_resource_exhausted`: the
*taxonomy* is public contract and does not grow for distinctions that fit inside
it. What distinguishes them is `ErrorInfo.details["state"]` and
`["cost_spent"]`, plus the `decision` field on the audit record (`"cost"`,
`"concurrency"`, `"queue_full"`, `"queue_timeout"`, `"rate"`, `"timeout"`,
`"cancelled"`, `"cancellation_requested"`).

`plugin_host_error` exists to keep three different situations apart: *policy said
no* (`plugin_capability_denied`), *the plugin asked wrongly*
(`plugin_invalid_input`), and *the host broke*. Collapsing the third into either
of the first two produces an audit trail that blames the wrong party — and, in
the case of a failed capability, used to record `decision="allow"` next to a
failure status, which reads as success to anything scanning the log.

A denial is sticky: `invoke()` re-checks the recorded host calls after the module
returns, so a plugin that receives `-2` and then answers `{"ok": true}` is still
reported as `plugin_capability_denied`. That is the difference between "the
plugin behaved" and "the plugin was allowed to behave".

## Resource limits

| Limit | Enforced by | Failure mode |
| --- | --- | --- |
| CPU (deterministic) | `Config.consume_fuel` + `Store.set_fuel` | `OUT_OF_FUEL` trap |
| Wall clock | `Config.epoch_interruption` + `set_epoch_deadline` + `Engine.increment_epoch()` | `INTERRUPT` trap |
| Linear memory | `Store.set_limits(memory_size=...)` | `memory.grow` returns `-1`; a write past the end traps; a module whose *minimum* exceeds the cap is refused at instantiation |
| Input / output size | `payload` length check, `max_output_bytes` clamp | `plugin_invalid_input` / `plugin_invalid_output` |
| stdio volume | `_BoundedSink`, 8 KiB | truncated capture, no host allocation |
| Host-call duration | `HostExecutor` timeout | the call reports `timed_out`; the capability is **not** stopped — see [host-capability-execution.md](host-capability-execution.md) |
| Host-call count | `_HostBridge.enter()`, counted per attempt | `BUDGET_EXHAUSTED` (-7), invocation fails as `plugin_resource_exhausted` |
| Host-call cost | `HostExecutor` charges the capability's declared `cost` against `limits.max_cost` on admission | `COST_EXHAUSTED` (-8), invocation fails as `plugin_resource_exhausted` |
| Host-call concurrency | four counters in `HostExecutor` (per invocation, per plugin, global, blocking pool) | `CONCURRENCY_LIMIT` (-9) when the queue is also full; otherwise the call *waits* |
| Admission queue | `max_waiters` (16) and `max_queue_wait_s` (0.25 s) in `HostExecutor` | `CONCURRENCY_LIMIT` (-9) with `decision="queue_full"`, or `QUEUE_TIMEOUT` (-13) with `decision="queue_timeout"`; neither executes nor charges |
| Call rate | sliding window of `rate_window_s` (1 s) per `(plugin_id, capability)`, `rate_limit_per_s` declared per capability | `RATE_LIMITED` (-12), `decision="rate"`; the check runs before cost and before admission |

Manifest values are **clamped by ceilings in code** (`MAX_TIMEOUT_MS` 30 s,
`MAX_FUEL` 5e9, `MAX_MEMORY_BYTES` 256 MiB, `MAX_PAYLOAD_BYTES` 1 MiB,
`MAX_HOST_CALLS` 1024, `MAX_COST` 4096). A plugin cannot widen its own budget by
declaring a larger number — a manifest asking for more is a validation error, not
a negotiation.

**A full host queues briefly before it refuses.** Until Phase 2.5, admission was
refuse-when-full and first-come: a plugin holding every slot kept everyone else
out until it finished, at zero cost to itself. Now a call that finds no capacity
parks in a bounded queue (16 waiters) for at most `max_queue_wait_s` (0.25 s) and
is dispatched round-robin across plugins, so a backlog owned by one plugin cannot
bury another plugin's first call. The claim is **bounded waiting, not strict
fairness**: there is no weighting, no latency guarantee, and the whole mechanism
is per host instance. Setting `max_waiters=0` restores the old behaviour exactly,
which is what the Phase 2.4 tests do when they mean to test the slot rather than
the wait.

**A timeout is not a cancellation.** The executor reports six states
(`refused`, `completed`, `failed`, `timed_out`, `cancelled`,
`cancellation_requested`) and only the fifth means the work stopped. A
synchronous capability can only ever be timed out; an `async def` capability can
be cancelled, and the host waits out a grace period before claiming it was.
Measured: a 250 ms blocking capability used to hold the caller — and the whole
event loop — for 256 ms under a 50 ms budget. It now returns at the deadline and
keeps running in the bounded pool. See
[host-capability-execution.md](host-capability-execution.md) for the limits, the
counters and the shutdown behaviour.

## Pitfalls worth knowing before editing this package

Each of these was found by a failing test, and each fails *silently* in the
direction of "no guarantee" if you get it wrong.

1. **`wasmtime.Trap` is not a `wasmtime.WasmtimeError`.** Sibling classes under
   `Exception`. Catch both, or the trap is the one thing you miss. The module
   defines `WASMTIME_FAILURES` for exactly this; use it instead of naming either
   class at a call site.
2. **An exception raised inside a host callback is not constrained by
   Wasmtime.** It travels back through the guest call, through `_run`, and out of
   `invoke()` unless something catches it. That is why `dw_host_call` has no bare
   code path and why `invoke()` ends with a containment net.
3. **`memory.read` past the end truncates silently; `memory.write` raises
   `IndexError`.** Neither is a guarantee you should rely on across versions —
   `_guest_write` checks bounds itself and then also catches the exception.
4. **`epoch_interruption=True` arms the deadline at the current epoch.** Without
   `store.set_epoch_deadline(1)` the module traps during ABI verification. One
   tick of headroom is required.
5. **`linker.define_func(..., access_caller=True)` passes `caller` first.**
6. **`WasiConfig.stdout_custom` wants a callback**, not a file-like object.
7. **`memory.grow` returning `-1` is the memory cap working.** It is not an
   error signal to the host; only an out-of-bounds *write* traps.
8. **Export signatures are checked at load, from `module.exports[i].type`.**
   Checking names only was not enough: a module exporting `dw_alloc` with the
   wrong arity loaded fine and failed later as a `TypeError` in the middle of an
   invocation — reachable before any authorisation.
9. **A capability must never run on the event loop thread.** `dispatch` is a
   coroutine on the loop, so `handler.run(payload)` called directly from it
   blocks every other coroutine for the duration of the call. This is what Phase
   2.4 fixed, and `event-loop-offload` is the mutation that proves it stayed
   fixed.
10. **`future.cancel()` returns `False` for a running thread.** It is not an
    error and not a stop. Treating the return value as "cancelled" is how a
    timeout gets laundered into a cancellation.
11. **A coroutine that swallows `CancelledError` keeps running.** After
    `task.cancel()` you must wait for the task before claiming it stopped — hence
    `cancellation_requested`, which exists precisely for that case.
12. **`asyncio.wait_for` cancels the *waiter*, not the worker.** A queued call is
    a future; wrapping the wait in `wait_for` means a timeout cancels a future
    that may have just been granted a slot, losing the slot while the caller
    believes it timed out. The queue uses `asyncio.wait(..., timeout=...)` and
    inspects `done`/`pending` so a granted-but-unconsumed future is never
    destroyed. Mutation `queue-fairness` and the queue-timeout test cover this.
13. **Rotation must happen on dispatch, not on arrival.** Round-robin is
    `_rotate_locked(plugin_id)` *after* a waiter is served. Rotating when a
    request arrives just records who asked last, which is the same as
    first-come — the queue would look fair and behave unfairly.
14. **A refusal must not charge.** Cost is charged on admission, not on enqueue.
    Charging at enqueue time makes a plugin that is being starved *also* poorer,
    so the queue turns a liveness problem into a permanent one. Mutation
    `queue-refusal-not-charged` exists to catch exactly this regression.
15. **Rate, cost and capacity are three different counters.** Rate limits calls
    per second (`rate_limit_per_s`); cost prices a call (`cost`); capacity is how
    many run at once. None derives from the others: a cheap capability called
    10 000 times per second is a rate problem, an expensive capability called
    once is a cost problem, and confusing them means one of the two attacks has
    no defence.

## Deliberately not implemented

- A `Store` pool. Instantiation is ~1 ms per call, which is fine at the current
  scale and the wrong thing to optimise first.
- Dynamic cost prediction. Cost is a static declared weight per capability; a
  capability whose cost depends on its input cannot express that yet.
- Preemption of a synchronous capability. The host can stop waiting, not stop
  working. That needs a process and a kill — see ADR 0016 Decision 4 for the
  evaluation and the trigger that would force it.
- Strict fairness, weighted shares, or priority classes. The queue is
  round-robin with no weights; a plugin that calls a thousand times per turn gets
  no more turns than one that calls once. Anything stronger is a scheduler, and a
  scheduler needs a requirement that does not exist yet.
- Cross-host admission. Rate windows and queues live in one `HostExecutor`; two
  hosts in one process share nothing.
- Wiring a plugin into `Runtime` as an adapter. That is a migration, and this
  phase exists to decide whether it is worth doing.
