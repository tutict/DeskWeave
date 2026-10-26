# deskweave-plugin-wasm

WASM/WASI plugin runtime for DeskWeave: loads `wasm32-wasip1` modules, runs them
under enforced limits, and mediates every host capability through the existing
permission layer.

This package is a **feasibility prototype**, not a plugin marketplace. What it
proves:

- a plugin compiled from another language (the example is Rust) can be loaded,
  invoked and validated without touching the Python runtime's internals;
- the sandbox has no filesystem, network or process access — WASI is defined with
  no preopened directories and an empty environment;
- CPU, wall-clock and memory budgets are enforced by Wasmtime (fuel, epoch
  interruption, allocator cap) and verified by tests that actually exhaust them;
- a plugin cannot reach a host capability it did not declare, and cannot obtain
  one by declaring it — authorisation goes through
  `deskweave_adapter_sdk.PermissionChecker`, the same layer that guards the
  browser and Blender adapters;
- the host-call channel survives a hostile plugin: Phase 2.3 wrote 66 tests that
  only exist to break it, found five real escapes (a plugin could make
  `invoke()` raise `IndexError`, `RecursionError` or `TimeoutError` without ever
  holding a grant), fixed each at the layer that owned it, and added a
  per-invocation host-call budget (`limits.max_host_calls`). Phase 2.4 then took the
  capability off the event loop, bounded its pool, priced it by weight and made
  `timed_out` and `cancelled` different words. Phase 2.5 made the module's identity
  checkable (`wasm_sha256`, `deskweave.install.json`, lexical path rules) and
  replaced first-come admission with a bounded queue served round-robin plus
  optional per-capability rate limits. Phase 2.6 added the missing half — a
  plugin can no longer authorise itself: `requested`, `approved` and `granted` are
  three separate sets, only `approve()` creates grants, dispatch requires an
  `ALLOW` that came from a grant or a one-shot, re-pinning clears the approval, and
  revocation deletes pending one-shots too. Phase 2.6.2 made the decision stay
  true: a narrowed approval deletes the grant it dropped, a call queued before a
  revocation never runs, a no-op reinstall writes nothing, and a grant alone no
  longer authorises a plugin call. 285 tests pass in the plugin suites;
  `scripts/wasm_guard_mutation_check.py` deletes each control in turn and
  requires the suite to go red (32/32 caught).

Four limits, stated here rather than left to be discovered:

- **A hash is not a signature.** The pin detects that the bytes changed. It says
  nothing about who wrote them, and an attacker who can rewrite the module can
  rewrite the manifest and the record to match. There is no publisher key and no
  revocation list.
- **An approval is a local record, not a credential.** It lives beside the grants
  under the same OS user, so anyone who can write it can approve anything. What it
  prevents is *unattended* authority — a manifest granting itself — not a local
  attacker.
- **Fairness is bounded waiting.** The queue holds 16 waiters for at most 0.25 s
  and rotates between plugins. There is no weighting, no latency guarantee, and
  every counter is per `WasmPluginHost` instance — two hosts share nothing.
- **Revocation stops the next call, not the current one.** A running capability
  completes; `RevocationReport` says `queued_stopped=N`, `running=M,
  stopped=False` — the calls that had not been admitted were refused, the ones
  that had were not interrupted.

What it is **not**: WIT / Component Model. `wasmtime-py` 50.0.1 binds only core
WebAssembly, so the prototype defines a minimal, language-agnostic
length-prefixed JSON ABI instead. The mapping to WIT and the conditions for
migrating are recorded in `docs/decisions/0014-wasm-plugin-isolation.md`.

See:

- `docs/architecture/wasm-trust-boundary.md` — layer by layer: what is untrusted,
  what must be validated, and which layer a bug belongs to;
- `docs/architecture/wasm-plugin-runtime.md` — the call chain and the ABI;
- `docs/architecture/wasm-threat-model.md` — what is and is not contained, filed
  as *verified* / *depends on the host* / *not guaranteed* / *not verified here*;
- `docs/architecture/wasm-capability-catalogue.md` — everything a plugin may ask
  the host to do;
- `docs/architecture/plugin-installation.md` — install records, hash pinning, path
  rules, and what a pin does *not* prove;
- `docs/decisions/0016-plugin-provenance-and-fairness.md` — why content pinning
  instead of a PKI, and why a bounded queue instead of a scheduler;
- `docs/architecture/plugin-lifecycle.md` — approval, activation, revocation, and
  the three permission sets;
- `docs/decisions/0017-plugin-approval-boundary.md` — why provenance went into the
  contract instead of the plugin host;
- `docs/development/writing-a-wasm-plugin.md` — build your own.
