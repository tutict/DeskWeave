# The WASM plugin trust boundary, layer by layer

This document traces one invocation from untrusted bytes to an audit record and
states, for each layer, what is actually enforced. It is written against the code
in `packages/plugin-wasm` after the Phase 2.4 audit, and every row names the test
that proves it.

Two rules made this document necessary:

- **A design intent is not a protection.** Where the comment said "the host
  validates X" but nothing did, the row says so.
- **"Containment" is not "prevention".** Several layers cannot stop a hostile
  plugin from trying something; they can only ensure the attempt produces a
  defined result. Those rows say *contained*, not *blocked*.

Legend for the status column:

| Status | Meaning |
| --- | --- |
| **enforced** | Implemented and proven by a test that fails when the guard is removed |
| **enforced (2.3)** | Was missing or incomplete before Phase 2.3; added and proven now |
| **enforced (2.4)** | Added by the execution-governance phase; proven by a test and a mutation |
| **enforced (2.5)** | Added by the provenance/fairness phase; proven by a test and a mutation |
| **enforced (2.6)** | Added by the approval-boundary phase; proven by a test and a mutation |
| **contained** | The hostile action may happen, but its effect is bounded and reported |
| **gap** | Not implemented. Listed so it is not mistaken for a protection |

## The chain

```
plugin bytes ──► load()  ──► Store/instance ──► dw_invoke ──► dw_host_call ──► decode
   (1)             (2)            (3)              (3)           (4)(5)         (6)
   └── approval guard (1b) ──────┘                                                  │
                                                                                    │
   InvocationResult ◄── audit ◄── write ◄── encode ◄── capability ◄── admission ◄ permission
         (12)          (11)       (10)       (9)          (8)          (7)        (6)
```

### 1. Plugin bytes and manifest — `load()`

| | |
| --- | --- |
| **Untrusted input** | The `.wasm` bytes and the `.json` manifest. Both are attacker-authored. |
| **Trust boundary** | Entry to the host. Nothing from here has been validated yet. |
| **Must validate** | Manifest parses as JSON and satisfies `WasmPluginManifest` (`extra="forbid"`); a declared `wasm_path` is relative, `..`-free and resolves inside the directory; **the bytes hash to the declared/recorded `wasm_sha256`** when the host requires integrity; `interface_version` equals `ABI_VERSION`; the module is loadable; the required exports exist **with the ABI signatures**; `dw_abi_version()` reports the current ABI. |
| **On failure** | `plugin_not_found`, `plugin_malformed`, `plugin_abi_mismatch`, `plugin_integrity`. No instantiation of the *invocation* path. |
| **Can it bypass authorisation?** | No — this layer decides nothing about capabilities, and a manifest's `requested_capabilities` is never converted into a grant. |
| **Status** | **enforced** for names, versions and manifest shape. **enforced (2.3)** for signatures: before, `dw_alloc` with the wrong arity raised out of the host's own call frame. **enforced (2.5)** for content identity and path containment, opt-in via `require_integrity` / `require_install_record`; default mode records the digest without enforcing it. |
| **Tests** | `test_wasm_manifest.py`, `test_wasm_plugin_contract.py`, `test_wasm_authorisation_adversarial.py::test_abi_signature_mismatch_is_a_load_error`, `::test_trap_during_instantiation_is_a_load_error`, `test_wasm_integrity.py` (23), `test_wasm_install_integrity.py` (14) |

Integrity is a **content** check: it detects a modified or swapped module, and it
says nothing about who produced it. See
[plugin-installation.md](plugin-installation.md) §8 before quoting it as
"verified plugin".

Instantiation itself runs guest code (`start` sections, data initialisers), so
`load()` and `_verify_abi_version()` must both catch traps. They previously
caught only `wasmtime.WasmtimeError`, which is a **sibling** of `wasmtime.Trap`,
not a parent — so a module whose `start` traps made `load()` raise a raw `Trap`.

### 1b. The approval guard — inside `load()`, before instantiation

| | |
| --- | --- |
| **Untrusted input** | The manifest's declared capabilities, its version and ABI version — all attacker-authored. |
| **Trust boundary** | Host policy, applied to a build that has already been hashed. |
| **Must validate** | A current approval exists for this plugin; its digest, version, ABI version and capability set still cover this build. The guard runs **after** hashing and **before** the engine is asked to parse or instantiate. |
| **On failure** | `plugin_not_approved` with the specific reason (`content changed`, `version changed`, `ABI changed`, or the `added` capability list) and `requires_reapproval=True` when a reinstall erased the decision. |
| **Can it bypass authorisation?** | It **is** the authorisation gate at load time. A host constructed without a guard has no approval check — which is why `PluginLifecycle` builds its own host instead of accepting one. |
| **Status** | **enforced (2.6)**, when a lifecycle owns the host. `WasmPluginHost()` used directly is unchanged and unguarded by design (existing callers and tests). |
| **Tests** | `test_plugin_lifecycle_runtime.py` (15), `test_plugin_lifecycle.py::test_activation_without_approval_is_refused`, mutation `activation-approval` |

The guard's placement is a security property, not a convenience: instantiating
first would run `start` sections and data initialisers of a module nobody
approved. See [plugin-lifecycle.md](plugin-lifecycle.md) §4.

### 2. Request payload — `_prepare_payload`, before the module runs

| | |
| --- | --- |
| **Untrusted input** | The `payload` dict supplied by the caller to `invoke()`. In practice mostly host-built, but treated as untrusted. |
| **Trust boundary** | Host → guest. |
| **Must validate** | `input_schema` (Draft 2020-12) and `max_input_bytes`. |
| **On failure** | `plugin_invalid_input`. The module is never instantiated. |
| **Can it bypass authorisation?** | No. |
| **Status** | **enforced** |
| **Tests** | `test_wasm_plugin_runtime.py::test_input_that_violates_the_schema_never_reaches_the_module`, `test_wasm_invariants.py::test_input_limit_is_enforced_before_the_module_runs` |

### 3. Guest execution — `_run`

| | |
| --- | --- |
| **Untrusted input** | Arbitrary guest code. |
| **Trust boundary** | Inside the sandbox. Everything the guest can do is bounded here. |
| **Must enforce** | Fuel (`OUT_OF_FUEL`), wall clock (`INTERRUPT`), linear-memory cap. One `Store` per invocation, one `Engine` per plugin. No preopened directories, empty environment, captured stdio. |
| **On failure** | `plugin_resource_exhausted`, `plugin_timeout`, `plugin_trapped`, `plugin_malformed`. The invocation fails; the host survives. |
| **Can it bypass authorisation?** | No. It cannot reach the host except through `dw_host_call`. |
| **Status** | **enforced** (measured, see ADR 0014) |
| **Tests** | `test_wasm_plugin_negative.py` (18), `test_wasm_invariants.py::test_each_declared_limit_is_enforced` |

One more guard lives here: `dw_alloc` is guest code, so its return value is an
arbitrary `i32`. Writing the request frame at that address used to be an
unchecked `memory.write` — a plugin returning `-1` made the host raise
`IndexError` **before any authorisation happened**.

### 4. `dw_host_call` entry — the single import

| | |
| --- | --- |
| **Untrusted input** | Four `i32`s: request pointer and length, output pointer and capacity. |
| **Trust boundary** | Guest → host. This is *the* boundary. |
| **Must validate** | `req_len` within `(0, max_input_bytes]`; `out_cap > 0`; both pointers inside linear memory; every attempt counted against the host-call budget. |
| **On failure** | A negative `HostCallStatus`; the refusal is recorded in the audit so the plugin cannot hide it. |
| **Can it bypass authorisation?** | No, but it can *attempt* anything — that is what layer 6 exists for. |
| **Status** | **enforced** for range checks and pointer validity. **enforced (2.3)** for counting: the budget previously counted dispatches, so a loop of refused calls was free. |
| **Tests** | `test_wasm_authorisation_adversarial.py::test_request_length_beyond_the_limit_is_refused`, `::test_request_pointer_outside_memory_is_refused`, `test_wasm_host_budget.py::test_malformed_calls_consume_the_same_budget` |

### 5. Request decoding

| | |
| --- | --- |
| **Untrusted input** | Bytes read from guest memory. |
| **Trust boundary** | Host. Still inside the callback, so an exception here escapes through Wasmtime. |
| **Must validate** | Read length equals the declared length; bytes decode as UTF-8; the document decodes as JSON **without unbounded recursion**; the result is an object with a string `capability` and an object `payload`. |
| **On failure** | `INVALID_ARGUMENT` (recorded). |
| **Can it bypass authorisation?** | No. |
| **Status** | **enforced (2.3)** for recursion and short reads. `RecursionError` is not a `ValueError`, and a deeply nested request used to unwind out of `invoke()` entirely. |
| **Tests** | `test_wasm_authorisation_adversarial.py::test_malformed_request_is_refused_and_audited` (11 shapes), `::test_deeply_nested_request_is_refused_not_crashed` |

### 6. Capability selection and authorisation — `HostCapabilityRegistry.dispatch`

| | |
| --- | --- |
| **Untrusted input** | A capability name and a payload dict, both chosen by the plugin. |
| **Trust boundary** | Host policy. |
| **Must validate** | In order: the capability exists in this build; the plugin **declared** it; the payload matches the capability's JSON Schema; the **existing** `PermissionManager` authorises `wasm-plugin:<id>` for it; **that ALLOW carries provenance** — `via ∈ {GRANT, ONE_SHOT}`; and, when a lifecycle supplies one, that a **current approval covers this capability for this build** (`dispatch_guard`). No checker wired means deny. |
| **On failure** | `DENIED_UNKNOWN_CAPABILITY`, `DENIED_NOT_DECLARED`, `INVALID_ARGUMENT`, `DENIED_NO_GRANT` (including the `policy_default` and `not_approved` refusals). |
| **Can it bypass authorisation?** | **No.** The checks are sequential and each returns before the next; the handler is only reachable through the last one. There is no second permission system to fall out of sync with. |
| **Status** | **enforced**, plus **enforced (2.6)** for provenance: an `ALLOW` produced by policy (not by a grant or a one-shot) is refused, so a manifest declaring a read-only capability cannot authorise itself. **Enforced (2.6.2)**: a grant is not an approval — a hand-written grant for a capability the approval never covered is refused as `not_approved`. |
| **Tests** | `test_wasm_host_capability.py` (10), `test_wasm_authorisation_adversarial.py` (first five cases), `test_plugin_authorisation_boundary.py` (6), `test_plugin_approval_consistency.py::test_a_hand_written_grant_does_not_authorise_a_plugin_call`, mutations `grant-provenance` and `dispatch-approval` |

This is the layer where "the plugin asked for it" and "the plugin may have it" are
separated, and the separation is the whole design: a `PermissionRequest` with
`scope=PLUGIN_HOST_CALL` and risk `LOW` — never `READ_ONLY`, because read-only
capabilities are auto-allowed by the shared policy.

Phase 2.6 added the second half of that separation. Risk level alone was not
enough: the field that says *which* decision produced an `ALLOW` did not exist, so
"policy allowed it" and "a person granted it" arrived at dispatch looking
identical. `DecisionVia` distinguishes `GRANT`/`ONE_SHOT` from `POLICY`/`DENY`/
`CONFIRMATION`, and an unset provenance is refused as unnamed rather than
defaulted to trusted. Details: [plugin-lifecycle.md](plugin-lifecycle.md) §5.

### 7. Execution admission — `HostExecutor.run`

| | |
| --- | --- |
| **Untrusted input** | How *many* calls the plugin makes, and how expensive each one is. Both are chosen by the plugin. |
| **Trust boundary** | Host capacity. |
| **Must validate** | In order: the host is not shutting down; the invocation is not cancelled; **this plugin's approval has not been withdrawn**; the per-plugin rate window has room; the invocation's cost budget can cover this capability's declared weight; the invocation has fewer than `max_concurrent_per_invocation` calls in flight; the plugin fewer than `max_outstanding_per_plugin`; the host fewer than `max_concurrent`; a synchronous call has a free blocking worker. A capacity refusal parks the call in a bounded queue; everything else refuses. Then charge and reserve, in one critical section. |
| **On failure** | `RATE_LIMITED` (-12), `COST_EXHAUSTED` (-8), `CONCURRENCY_LIMIT` (-9, including a full queue), `QUEUE_TIMEOUT` (-13) or `DENIED_NO_GRANT` with `decision="revoked"`. Nothing executes, nothing is charged, and the refusal is in the audit. |
| **Can it bypass authorisation?** | **No** — admission runs *after* the permission check and can only refuse, never widen. Since 2.6.2 it is also the **linearisation point of a revocation**: a call queued before the decision is removed from the queue and refused, so "authorised a moment ago" is not the same as "runs now". |
| **Status** | **enforced (2.4)** for cost and concurrency, **enforced (2.5)** for rate, queue bounds and round-robin ordering, **enforced (2.6.2)** for revocation at admission |
| **Tests** | `test_wasm_host_execution.py` (cost, concurrency, pool cap), `test_wasm_host_budget.py`, `test_wasm_fair_scheduling.py` (15), `test_plugin_approval_consistency.py::test_a_queued_call_never_runs_after_the_approval_is_withdrawn`, mutation `revocation-drains-queue` |

This layer did not exist in Phase 2.3. Without it, a plugin that was granted one
capability could call it 64 times per invocation and as many invocations as the
caller allowed, with no bound on how many ran at once. Phase 2.5 adds the
fairness half: without a queue, a single plugin holding every slot refuses
everyone else indefinitely without ever exceeding its own quota.

### 8. Capability implementation — `HostCapability.run`

| | |
| --- | --- |
| **Untrusted input** | The plugin's payload, already schema-validated. |
| **Trust boundary** | Inside the host process, with full host authority. |
| **Must validate** | Nothing further is *enforced*: this is trusted code. Its contract is that it returns data and performs no side effect the permission layer did not authorise. |
| **On failure** | The exception is caught in the executor, logged with a traceback, and reported as `HOST_ERROR` / `plugin_host_error` with `state="failed"`. The plugin never sees a traceback, a path or a token. |
| **Can it bypass authorisation?** | **Out of scope** — a capability is host code. A bug here is a vulnerability with no second layer behind it. |
| **Status** | **contained** (exception handling, bounded pool, off the event loop), **gap** (no sandbox around capabilities; a synchronous one cannot be interrupted) |
| **Tests** | `test_wasm_host_budget.py::test_capability_exception_becomes_a_host_error_not_a_denial`, `test_wasm_host_execution.py::test_a_capability_failure_is_audited_as_admitted_but_failed` |

### 9. Response encoding

| | |
| --- | --- |
| **Untrusted input** | Whatever the capability returned. |
| **Trust boundary** | Host → guest. |
| **Must validate** | Serialisable to JSON; size within `min(out_cap, max_output_bytes)`. |
| **On failure** | `BUFFER_TOO_SMALL`, recorded. |
| **Status** | **enforced** |
| **Tests** | `test_wasm_authorisation_adversarial.py::test_output_capacity_smaller_than_the_response_is_refused` |

### 10. Write back into guest memory

| | |
| --- | --- |
| **Untrusted input** | `out_ptr` and `out_cap`, both plugin-chosen. |
| **Trust boundary** | Host → guest. |
| **Must validate** | `out_ptr >= 0`; `out_ptr + len(body) <= memory.data_len()`. |
| **On failure** | `BUFFER_TOO_SMALL`, recorded, and the invocation fails. |
| **Can it bypass authorisation?** | No, but an unchecked write let a plugin turn a host success into a host **crash**: `out_ptr = -1` raised `IndexError` through Wasmtime, `_run` and `invoke`. |
| **Status** | **enforced (2.3)** |
| **Tests** | `test_wasm_authorisation_adversarial.py::test_writable_buffer_at_a_negative_pointer_is_refused` |

### 11. Audit record — `HostCallOutcome`

| | |
| --- | --- |
| **Untrusted input** | None directly; it records what happened at layers 6–9. |
| **Must satisfy** | Every attempt appears exactly once, including refusals, timeouts and budget exhaustion; a refusal is never dropped, because a dropped refusal is an invitation to ignore it. `state` is required and always one of `CallState`: since Phase 2.6.2 a record cannot say "something happened" without saying what. |
| **Status** | **enforced (2.3)** for refusals and timeouts. Before, a refused call produced no record, so a plugin that ignored the status code could report success and be believed. **Enforced (2.6.2)**: the budget marker and every pre-dispatch refusal carry `state="refused"` instead of an empty string. |
| **Tests** | `test_wasm_host_budget.py::test_budget_record_is_written_once_however_many_calls_follow`, `::test_host_call_timeout_is_contained_and_audited`, `test_wasm_plugin_negative.py::test_a_failed_plugin_cannot_land_a_task_in_succeeded`, `test_plugin_approval_consistency.py::test_a_budget_refusal_records_a_state`, `::test_a_request_refused_before_dispatch_records_a_state` |

### 12. Result — `InvocationResult`

| | |
| --- | --- |
| **Untrusted input** | The guest's response frame and stdout. |
| **Must validate** | Frame length ≤ `max_output_bytes`; body is JSON; body is an object; matches `output_schema`; **no recorded denial** (a denial outranks a happy payload). |
| **On failure** | `plugin_invalid_output`, or the first recorded denial. `ok` is `False` and `data` is `None`. |
| **Status** | **enforced**, plus a **containment net** that converts any unanticipated host exception into a failed invocation rather than an exception at the call site. |
| **Tests** | `test_wasm_invariants.py::test_failure_is_never_reported_as_success` (8 modes), `test_wasm_host_capability.py::test_plugin_that_swallows_a_denial_cannot_report_success`, `test_wasm_host_budget.py::test_an_unexpected_host_failure_is_contained` |

## Where the boundary is *not*

- **Capabilities run with full host authority.** Payload validation bounds the
  *shape* of the arguments, not their meaning: a future `read_file(path)` would
  let a plugin name any path. The sandbox protects the channel, not the argument.
- **Timing.** Fuel is deterministic; the wall clock is approximate.
- **Cancellation.** A synchronous `HostCapability.run` is now offloaded to a
  bounded thread pool, so it no longer holds the event loop — but it still cannot
  be interrupted. A timeout ends the *wait*, not the work: the thread runs to
  completion and keeps its concurrency slot until it returns. Measured:
  `test_wasm_host_execution.py::test_a_sync_capability_times_out_and_keeps_running`,
  `test_wasm_host_execution.py::test_cancelling_the_invocation_does_not_claim_a_sync_capability_stopped`.
- **Provenance.** A SHA-256 pin (opt-in) detects a changed or swapped module; it
  does **not** identify the author. A manifest and an install record that an
  attacker can both rewrite are not a root of trust, and nothing here signs
  anything. See [plugin-installation.md](plugin-installation.md) §8.
- **The approval store.** An approval is a local record of a local decision,
  written next to the grants under the same OS user. Anyone who can rewrite it can
  approve anything — the same power they already had over the permission table.
  It is a control against *unattended* authority, not against a local attacker.
- **Revocation stops the next call, not the current one.** `revoke()` deletes
  grants and discards pending one-shots; an operation already running completes
  and is reported as `still_running` with `stopped=False`. No preemption exists.
- **An unguarded host.** `WasmPluginHost()` constructed directly has no approval
  check. That is intentional (existing callers, and the negative tests that must
  observe pre-authorisation behaviour), but it means "the approval boundary" is a
  property of `PluginLifecycle`, not of the package as a whole. A runtime loader
  that instantiates a host itself must supply an `activation_guard`.
- **Fairness between plugins.** `plugin.lock` serialises calls to one plugin, and
  `HostExecutor` admits up to 8 in flight (4 per plugin, 2 per invocation, 4
  blocking) and parks capacity refusals in a 16-slot queue served round-robin.
  Waiting is **bounded, not strictly fair**: no weighting, no latency guarantee,
  and every counter is per process, so two hosts do not see each other's load.
- **A pinned file that was replaced together with its manifest.** Content
  identity cannot detect a substitution that also updates the pin. Only a
  signature over bytes the attacker cannot rewrite would, and that is not built.
