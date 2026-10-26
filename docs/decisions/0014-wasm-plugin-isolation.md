# ADR 0014 — A WASM plugin runs behind one host capability channel

- Status: accepted (prototype, not wired into `Runtime`)
- Date: 2026-10-23
- Phase: 2.2 (decision), 2.3 (authorisation and resource-budget verification)

## Context

Phase 2 asked whether a capability can be added without editing the runtime core.
Phase 2.1 answered it for *first-party* connectors: a new source is an enum
member, a package and one `register_collection_source` line. It left a harder
question open. Every connector so far is Python in this repository, written by
this project, reviewed by this project. What happens when the author is someone
else and the language is not Python?

The requirement was narrower than "a plugin system", and the narrowness is the
whole design:

- third-party code must be able to contribute a capability;
- it must **not** get file system, network or process access by default;
- it must not be able to hang, leak or crash the runtime;
- it must not require touching `Runtime`, the MCP gateway, the task state
  machine, `PermissionManager`, `ArtifactStore` or the existing adapters;
- it must not bypass the adapter boundary by reaching Blender or SQLite itself.

So the question is not "can we embed a scripting language". It is "what is the
smallest isolable unit we can execute, and what is the only door we have to give
it".

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Core WASM module + hand-rolled JSON frame ABI** (`wasm32-wasip1`, `wasmtime-py`) | Runs today on the existing host. Fuel metering, epoch interruption and a linear-memory cap are all engine features, not conventions. No OS-level sandbox needed. Any language that emits `wasm32-wasip1` works | ABI is ours to version and document. Two error hierarchies in `wasmtime-py` to get right (see §"What the prototype had to work around"). Instantiation is not free (~1 ms) |
| B. Component Model / WIT | The right long-term shape: typed interfaces, generated bindings, no hand-rolled framing | **Not executable on this host.** `wasmtime-py` 50.0.1 binds core WebAssembly only — no `Component` type, no component instantiation path, no WIT (measured: `hasattr(wasmtime, "Component") is False`). Choosing it would mean shipping a design that cannot be run |
| C. Python "plugins" imported in-process | Zero new machinery | No isolation at all. A plugin shares the interpreter, the SQLite connection and the `PermissionManager` object; "deny by default" becomes a naming convention. This is the thing the requirement forbids |
| D. One subprocess per plugin | Real process boundary, any language bundled as a binary | No deterministic CPU bound (only kill, which is slow and coarse). Needs an IPC protocol *plus* a platform-specific sandbox (Job Objects / `seccomp` / `sandbox-exec`) to stop FS access — three mechanisms instead of one, and the "no file system" guarantee becomes OS-dependent |
| E. Native shared library (`cdylib`) | Fastest call path | A `.so` is the host process. No isolation whatsoever; one `open()` and the sandbox story is over |
| F. Reuse the existing Node/Tauri side (JS sandbox) | Already in the stack | Puts plugin execution in the UI process, and forces every plugin author into JavaScript — which defeats the point of a language-agnostic surface |

Option B was rejected on evidence, not preference. Option A is B's fallback
shape, and the migration conditions back are written down below.

## Decision

1. **The unit of isolation is a core WebAssembly module.** `wasmtime` provides
   the three limits we need as engine features, so enforcement does not depend
   on the plugin cooperating: fuel (deterministic instructions), epoch deadlines
   (wall clock), `Store` memory limits (linear memory).
2. **One ABI, four exports, one import.** A plugin provides `memory`,
   `dw_abi_version`, `dw_alloc`, `dw_free`, `dw_invoke` and imports exactly one
   function, `env.dw_host_call`. Everything crossing the boundary is a
   length-prefixed UTF-8 JSON frame, validated by JSON Schema on both sides.
   Nothing else is importable: a module that imports anything the linker does not
   define fails to instantiate.
3. **A manifest is data, not authority.** `requested_capabilities` is a
   declaration. Every call is authorised separately, and the audit subject is
   `wasm-plugin:<plugin_id>` — so a grant issued to an adapter id does not
   authorise a plugin, and a plugin cannot widen its own budget by asking for
   more in its manifest (`MAX_*` ceilings clamp it).
4. **The host never allocates inside the sandbox.** `dw_alloc` is called by the
   host only to hand the request in; for host calls the *plugin* supplies the
   output buffer and its capacity, and the host writes only if the response fits.
   Length claims are read from the 4-byte header before the body is copied, so a
   plugin cannot make the host allocate by lying about a size.
5. **A fresh `Store` per invocation, one `Engine` per plugin.** Linear memory
   cannot survive a call, and incrementing one plugin's epoch cannot interrupt a
   different plugin that happens to be running.
6. **No preopened directories, an empty environment, captured stdio.** WASI is
   defined because Rust's `std` links against it; that is the only reason. It is
   given nothing to reach.
7. **Failure is a value, not an exception.** `invoke()` returns
   `InvocationResult` with `ok=False` and a structured `ErrorInfo`; every raw
   Wasmtime exception is translated at the boundary. A host-capability denial
   outranks whatever the plugin did with it — a plugin that swallows `-2` and
   returns `{"ok": true}` is still reported as a failure.
8. **Nothing is wired into `Runtime`.** `packages/plugin-wasm` is a library that
   takes a `PermissionManager` and a registry. Making a plugin an adapter is the
   migration this phase was told not to perform.

## Measured behaviour

All figures from this machine, this commit, `wasmtime-py` 50.0.1,
`wasmtime` CLI not involved. Reproduced by
`tests/integration/test_wasm_plugin_negative.py`.

| Mechanism | Trigger | Observed | Notes |
| --- | --- | --- | --- |
| Fuel | `loop br 0` | `plugin_resource_exhausted`, `TrapCode.OUT_OF_FUEL`, 5.7 ms wall | Deterministic: the example plugin consumes **198,483** fuel on every run |
| Epoch | same loop, fuel set to 5e9 | `plugin_timeout`, `TrapCode.INTERRUPT`, 154.9 ms wall | 150 ms budget, ~5 ms overshoot — see "Limits" |
| Memory cap, graceful | `memory.grow` past the cap | `memory.grow` returns **-1**; the module keeps running | A cap is not an error signal. The plugin must check for -1 |
| Memory cap, hard | write past linear memory | `TrapCode.OUT_OF_BOUNDS_MEMORY_ACCESS` | This is where corruption would have happened |
| Memory cap, static | module declares min 100 pages, cap 1 MiB | instantiation refused: `memory minimum size of 100 pages exceeds memory limits`, surfaced as `plugin_malformed` | Caught before a single instruction runs |
| Trap | `unreachable` | `plugin_trapped`, `UNREACHABLE` | Does not propagate |
| `proc_exit` | WASI exit | `plugin_trapped`, `details.exit_code` carries the number | Host process is unaffected; `wasmtime.ExitTrap` is translated like any other trap |
| Cancellation | `task.cancel()` | worker traps at the next epoch check, 0.2 ms | Runtime stays usable |

## What the prototype had to work around

These are documented because each one silently produces "no test fails, the
guarantee is absent" if you get it wrong.

- **`wasmtime.Trap` is not a `wasmtime.WasmtimeError`.** They are sibling
  subclasses of `Exception`. `except WasmtimeError` — the obvious guard for a
  best-effort cleanup — catches everything *except* the traps that path actually
  raises. The cleanup described below leaked a `Trap` out of `_run` until this
  was found by three failing negative tests.
- **Epoch interruption is armed at `Store` creation with the *current* epoch as
  the deadline.** With `epoch_interruption=True` and no `set_epoch_deadline`, the
  first check point traps immediately — the module traps during ABI
  verification. One tick of headroom (`set_epoch_deadline(1)`) is required.
- **`linker.define_func(..., access_caller=True)` passes the caller as the
  *first* argument**, not the last.
- **`WasiConfig.stdout_custom` takes a callback**, not a file-like object.

## Consequences

- A plugin cannot see the host. It has no ambient authority: no paths, no
  sockets, no environment, no clock beyond what `std` derives from WASI (which it
  cannot call usefully without preopens). Everything it achieves goes through
  `dw_host_call`, which is one auditable function.
- Adding a host capability is additive: implement the `HostCapability` protocol,
  register it with the registry. `runtime.py` does not change.
- The ABI is a durable external contract now. Removing an export is a breaking
  change and requires `ABI_VERSION` to move; a plugin built against a different
  `interface_version` is refused at load with `plugin_abi_mismatch`.
- The plugin surface is JSON-in/JSON-out. That is a real ergonomic cost for a
  typed language: a plugin author hand-writes `serde_json` plumbing. Accepted for
  the prototype because it is the only shape every candidate language already
  agrees on.
- **Instantiation is paid per invocation** (`Store` is per-call, ~1 ms for the
  99 KB example). Acceptable at the current scale; if a plugin is called in a
  tight loop this is the first thing to change, and the change is `Store`
  pooling — not the ABI.
- **A plugin cannot be trusted to be correct, only to be contained.** Containment
  is not correctness: a plugin that returns a well-formed but wrong answer will
  be believed. `output_schema` bounds the *shape*, not the truth.

## Limits — what this design does **not** protect against

Recorded here rather than in a footnote, because a sandbox that is trusted
beyond its actual reach is worse than no sandbox.

- **Timing.** Fuel is deterministic; the wall-clock deadline is not. Epochs are
  checked at compiled loop/prologue points, so a plugin can overshoot its budget
  by the check interval plus thread-scheduling delay. Measured: ~5 ms on a
  150 ms budget. A plugin cannot use this to run *indefinitely*, only to exceed
  its budget slightly.
- **Host-call cost is not metered.** Phase 2.3 added a per-invocation *count*
  (`limits.max_host_calls`, default 64, hard ceiling 1024, counted per attempt
  and returned as `-7 BUDGET_EXHAUSTED`). What a call *costs* is still not
  modelled: 64 browser launches are still 64 browser launches, because the
  plugin never pays for the capability's real work. Costing needs a price table
  and is not implemented.
  *Superseded in part by Phase 2.4/2.5:* a per-invocation **cost budget**
  (`limits.max_cost`, ceiling 4096) now prices each call at the weight the
  capability declares, and admission refuses before execution when the budget
  cannot cover it (`-8 COST_EXHAUSTED`). The original sentence still holds in the
  part that matters: the weight is **declared, not measured** — see
  [`../architecture/host-capability-execution.md`](../architecture/host-capability-execution.md)
  and the addendum below.
- **A granted capability's own runtime cannot be cancelled.** A synchronous
  `HostCapability.run()` runs to completion; a timeout means "we stopped waiting".
  Phase 2.4 removed the worse half — it no longer runs on the event loop, so the
  loop stays responsive — and added a real cancellation path for coroutine
  capabilities, plus a `cancellation_requested` state for the ones that ignore it.
  Interrupting a blocking function still requires a process and a kill; see
  [ADR 0015](0015-host-capability-execution.md).
- **Spectre-class side channels** are out of scope. Wasmtime does not claim to
  defend a plugin against a hostile *host*, and this design does not defend the
  host against a plugin that can measure cache timing.
- **Denial of service by volume.** Nothing limits how *many* invocations happen,
  how much total memory the process uses across plugins, or how much CPU the sum
  of all budgeted plugins consumes. Those are admission-control concerns and
  belong above this layer.
- **Native code is impossible to sandbox this way**, and the prototype gives a
  plugin no way to reach any (no `dlopen`, no FFI import). A future feature that
  grants one would dismantle the guarantee, not weaken it.

## Addendum — Phase 2.3: what adversarial verification changed

Phase 2.2 built the sandbox and proved the happy path. Phase 2.3 was written to
break it, and the design above was **not** sufficient on its own. Five distinct
escapes were real, each reachable from guest code, and each fixed at the layer
that should have owned it rather than at the call site that reported it.

| Escape | Fixed by | Decision-level consequence |
| --- | --- | --- |
| `dw_alloc` returning an out-of-bounds pointer made `invoke()` raise `IndexError`, **before any authorisation** | `_guest_write()` bounds-checks the destination and swallows the write exceptions | "Failure is a value" needed a mechanism, not just a convention. Every write into guest memory now goes through one helper |
| A negative `out_ptr` raised `IndexError` after authorisation had passed | Same helper, `ptr < 0` check | The guest chooses *where* the host writes. That is untrusted input even when the call was authorised |
| ~60 000 levels of nested JSON raised `RecursionError` out of `invoke()` | `json.loads` wrapped; `RecursionError` maps to `INVALID_ARGUMENT` | "Trust no claim about shape" has to include *depth*, not just size and type |
| A host call exceeding its timeout raised `TimeoutError` out of `invoke()` and lost the audit record | `FutureTimeoutError` handled in `_HostBridge`; outcome recorded before returning | A containment promise is only real where the exception is actually caught. The capability still cannot be cancelled (see Limits) |
| A trap in the `start` section or `dw_abi_version`, and a wrong export *signature*, raised bare `Trap` / `WasmtimeError` / `TypeError` out of `load()` | `WASMTIME_FAILURES = (WasmtimeError, Trap)` everywhere; export signatures checked structurally at load from `module.exports[i].type` | Load-time verification was checking names, not shapes. Checking names is not verification |

Two further design decisions came out of the same work:

- **Refusals are recorded by the host, not inferred from the return value.**
  `_HostBridge.reject()` writes the audit entry *before* the status code is
  returned, so a plugin that ignores `-2` and answers `{"ok": true}` still fails.
- **The host-call budget counts attempts, not successful dispatches.** Counting
  successes made malformed requests free, which turned the budget itself into a
  denial-of-service amplifier.

The general lesson, recorded because it will recur: the root cause of four of the
five escapes was a single wrong assumption — that `wasmtime.Trap` is a
`WasmtimeError`. It is not; they are siblings. A defensive pattern built on a
wrong class hierarchy fails silently, in the direction of "no guarantee".

## Migration conditions back to WIT / the Component Model

Revisit when **all** of these hold:

1. `wasmtime-py` exposes `wasmtime.Component` and component instantiation
   (verify with `hasattr(wasmtime, "Component")`, not with release notes).
2. The host still needs at least two guest languages — a one-language world does
   not justify a canonical ABI.
3. A concrete ABI break is on the table anyway (a breaking change is the only
   time the migration is free).

The framing layer (`abi.py`) is the only code that would be replaced: `runtime.py`
owns limits, the registry and the error taxonomy, and none of those change. The
manifest, capability registry, principal naming and threat model survive intact —
which is why it was worth pinning the *policy* before the *serialisation*.

## Tests

- `tests/unit/test_wasm_abi.py` — framing, length claims, the 1 MiB ceiling.
- `tests/unit/test_wasm_manifest.py` — `extra="forbid"`, id pattern, budget
  ceilings, capability de-duplication.
- `tests/contract/test_wasm_plugin_contract.py` — the committed `.wasm` exports
  exactly the five names, reports the current ABI, and imports nothing but
  `env.dw_host_call` and WASI `std`.
- `tests/integration/test_wasm_plugin_runtime.py` — the real Rust plugin through
  the real `PermissionManager`: denied without a grant, allowed after one, every
  host call audited, fuel bounded, two invocations sharing no memory.
- `tests/integration/test_wasm_host_capability.py` — unknown / undeclared /
  ungranted capabilities, one-shot grants, and a plugin that swallows a denial.
- `tests/integration/test_wasm_plugin_negative.py` — the nine failure classes plus
  a seam test proving a failed plugin lands a task in `failed`, never `succeeded`.
- `tests/integration/test_wasm_authorisation_adversarial.py` — 36 cases: capability
  selection and grant transfer, 11 malformed request shapes, pointer arithmetic,
  ABI and signature tampering, allocation outside memory, and state leakage
  between invocations.
- `tests/integration/test_wasm_host_budget.py` — 11 cases: exact budget boundary,
  per-attempt counting, single audit record, per-invocation (not per-plugin)
  scope, manifest cannot raise the ceiling, capability exception vs denial,
  timeout containment, and the honest record that a blocking capability cannot be
  cancelled.
- `tests/integration/test_wasm_invariants.py` — 19 cases covering the six
  invariants: failure never reports success, errors leak no path or token, no
  state survives, limits are enforced in practice rather than declared, and a
  failure still passes through the permission layer.
- `scripts/wasm_guard_mutation_check.py` — removes each control in turn and
  requires a red test. 8/8 caught, 0 skipped, working tree restored byte-for-byte
  (SHA-256 verified per file).

- `tests/integration/test_wasm_host_execution.py` — 14 cases for the four
  execution states, admission, cost accounting, slot lifetime and shutdown.

Total for the package at the end of Phase 2.4: **143 passed, 0 failed**; 13/13
guard mutations caught.

## Addendum — Phase 2.5: identity of the file, and who gets a turn

Two assumptions from Phase 2.2 survived until Phase 2.5 without being written
down, which is usually where a design is weakest:

1. **"The `.wasm` at that path is the plugin."** Phase 2.2 computed a SHA-256 at
   load and logged it. A log line is not a control: a replaced module produced a
   different digest in a line nobody read, and then ran. See
   [ADR 0016](0016-plugin-provenance-and-fairness.md) Decision 1 and
   [plugin-installation.md](../architecture/plugin-installation.md).
2. **"A plugin that holds a slot will release it."** Admission was first-come and
   refuse-when-full, so a plugin holding all four slots kept every other plugin
   out until it finished — at no cost to itself, with nothing in the design that
   was supposed to prevent it. See ADR 0016 Decision 2.

Neither fix changes the sandbox contract this ADR records: the ABI, the five
exports, the one import, the fuel/epoch/memory limits, and "failure is a value"
all stand. The pin is opt-in (`require_integrity`, `require_install_record`)
because every manifest written before 2.5 lacks a hash, and the queue is a
bounded 16-slot round-robin precisely so it does not become the scheduler this
ADR argues against. Package total at the end of Phase 2.5: **209 passed**;
20/20 guard mutations caught.

## Addendum — Phase 2.6: the sandbox never decided who may ask

Phase 2.2 through 2.5 built a boundary that answers "what can this code do" with
real precision and left "who said it may" entirely implicit. A plugin that
declared a capability was assumed to hold it, because the permission layer
auto-allows read-only risk — a policy written for adapters, applied to a party
that authors its own request. The manifest authorised itself.

That is not a sandbox defect: the sandbox was doing exactly what it promised. It
is a missing control at the layer above, and it is now
[ADR 0017](0017-plugin-approval-boundary.md): three permission sets that never
imply one another, an `activation_guard` inside `load()`, and a `DecisionVia`
provenance field so dispatch can tell a grant from a policy default.

Nothing this ADR records changes. The ABI, the five exports, the single import,
the fuel/epoch/memory limits and "failure is a value" all stand — the guard runs
before instantiation and returns the same `PluginError` taxonomy. Two limits
remain exactly as stated: a synchronous capability cannot be interrupted, and
there is still no publisher identity. Package total after the Phase 2.6.4
governance hardening: **341 passed** in the plugin suites;
41/41 guard mutations caught, 0 survived, 0 skipped — with the cross-process
store lock, the withdrawal mark and the activation snapshot each additionally
mutation-checked in dedicated runs (see plugin-lifecycle.md §12).
