# ADR 0016 — Plugin provenance, and fair scheduling under contention

- **Status:** accepted
- **Date:** 2026-10-23
- **Phase:** 2.5 (provenance and fairness), building on 2.2–2.4
- **Supersedes nothing.** Extends ADR 0014 (isolation) and ADR 0015 (execution governance).

## Context

Two gaps were open before a WASM plugin could be considered for a production
Runtime, and neither is a sandbox-escape problem:

1. **The host never checked that the file it loaded was the file it expected.**
   `load()` computed a SHA-256 and put it in a log line. The manifest could not
   declare a hash, and nothing compared one. A plugin replaced on disk after
   installation would run, and nothing would say otherwise.
2. **Admission was bounded but not fair.** Phase 2.4 refuses when full, so one
   plugin holding four blocking slots keeps every other plugin out indefinitely
   while staying inside its own quota. That is a denial of service with no
   attacker effort beyond a loop.

A third question was deferred rather than answered: a synchronous capability
cannot be interrupted, so should the host run untrusted synchronous work in a
separate **process**?

## Decision 1 — pin content, do not pretend to verify publishers

Options considered:

| Option | Why not chosen |
| --- | --- |
| Nothing (status quo) | an undetected swap is exactly the failure a plugin host must not have |
| SHA-256 pin (chosen) | cheap, offline, no key infrastructure; detects modification |
| Self-signed manifest signature | produces a green "signed" badge while the key ships next to the file — no added guarantee |
| Full PKI (root, rotation, revocation) | the key-distribution and revocation story does not exist; building it half-way is worse than not building it |
| Remote registry attestation | requires a service this phase is explicitly not building |

**Chosen:** an optional `wasm_sha256` in the manifest, plus an install record
that pins path, hash, version, ABI version and declared capabilities.

Consequences, stated so they cannot be read as more:

- the pin detects **content substitution**, never **authorship**;
- a manifest and record that an attacker can both rewrite are not a root of trust;
- `require_integrity` / `require_install_record` are opt-in, because every
  manifest written before this phase lacks a hash and must keep loading;
- a manifest that *adds* capabilities after being pinned is refused. That is a
  diff against a baseline, not an authorisation mechanism.

## Decision 2 — a small queue served round-robin, not a scheduler

Options considered:

| Option | Verdict |
| --- | --- |
| Refuse when full (Phase 2.4) | bounded, but a single plugin can starve everyone forever |
| Unbounded queue | turns a limit into a memory leak and hides latency in a counter nobody watches |
| Priority classes | needs a policy for who is important; this phase has no such input |
| Weighted fair queueing | more knobs than traffic; unverifiable without a workload model |
| **Bounded queue + round-robin (chosen)** | one mechanism, one bound, directly testable |
| Process-per-plugin | solves isolation, not scheduling; see Decision 4 |

**Chosen:** `max_waiters` (default 16) and `max_queue_wait_s` (default 0.25 s).
A call blocked only by **capacity** enqueues; cost, rate and cancellation are
decided before enqueueing. On release, the dispatcher serves waiters in
round-robin plugin order, moving a served plugin to the back so a backlog behind
one plugin cannot bury another plugin's first call. A waiter is woken **once**,
with a grant or a refusal — never to re-race, which is what makes the order mean
anything.

Admission order is: shutdown → cancellation → rate → capacity → cost. Shutdown
and cancellation are decided before a queue slot is spent; rate is decided before
capacity so a flood is refused cheaply instead of occupying queue space.

**What this guarantees:** bounded waiting, not strict fairness. A queued call
waits at most one in-flight operation plus `max_queue_wait_s`. There is no
latency SLA and no weighting, and `max_waiters=0` restores Phase 2.4's
refuse-fast policy for hosts that prefer it.

## Decision 3 — rate limits are a third, separate axis

`max_host_calls` counts attempts in one invocation. `max_cost` prices them.
Neither stops a plugin that makes one cheap call per second forever, so a rate
limit is needed — and it must not be confused with either.

**Chosen:** a sliding window keyed by `(plugin_id, capability)`, with the limit
coming from the capability (`rate_limit_per_s`) or the executor's default. A
capability's own limit wins, so an expensive operation can be tightened without
slowing everything else. Refusals happen before execution, are counted in the
audit, and cost nothing. Keys are per plugin, so one plugin's flood cannot spend
another plugin's allowance.

Explicitly **not** a cost model: it bounds frequency, not resource consumption.
An adversary can still make each permitted call expensive; that is what `cost`
and the execution timeout are for.

## Decision 4 — process isolation: evaluated, not implemented

Phase 2.4 established the fact that forces this question: no Python API can
interrupt a running function, so a synchronous capability runs to completion and
keeps its slot until it returns.

| Criterion | Thread pool (current) | Worker process | Capability service |
| --- | --- | --- | --- |
| Can be terminated | **no** | yes (kill) | yes (kill or drain) |
| Cross-platform | yes | yes, but spawn/forkserver differences and Windows handle inheritance need care | yes, plus deployment |
| IPC surface | none (in-process) | pickle/JSON over a pipe — a parser the host must trust | network auth, framing, versioning |
| Lifecycle | pool shutdown | spawn cost, orphan reaping, crash detection | service discovery, health, restart |
| Timeout / crash recovery | timeout only, no recovery | kill + respawn, loses in-flight work | supervisor + retry policy |
| Exposure | whatever the capability touches | **same files, network, env and credentials** — a subprocess is not a sandbox | whatever the service is granted |
| Packaging cost | none | platform-specific spawn, path to interpreter, frozen-app complications | a second deployable |
| Audit | in-process | result must be serialised back; failure modes double | results cross a network boundary |

**Verdict: not implemented.** The evidence does not support the cost yet:

- every capability in the catalogue is host-authored code, not plugin-supplied;
- the one shipped capability (`reference.metadata.validate`) is pure arithmetic;
- a subprocess does **not** remove the need for the other controls: it inherits
  the same credentials and filesystem, so "run it in a process" is a liveness
  fix, not a security boundary.

The trigger for revisiting is concrete: the first capability that (a) can run for
seconds and (b) is either third-party or performs blocking I/O the host cannot
bound. Until then the honest statement is "a synchronous capability cannot be
interrupted, and `aclose()` reports what was left running" — not "we isolate it".

## Consequences

- A host can now refuse a modified plugin instead of running it, and can say
  which check failed without printing a machine path.
- A plugin cannot starve the host by holding slots: it gets a bounded number of
  queued calls and can be outrun by round-robin.
- Three new status codes (`-12` rate, `-13` queue timeout) and two new error
  codes (`plugin_integrity`, `plugin_install`) extend, but do not renumber, the
  existing taxonomy.
- The ABI, `max_host_calls`, permission semantics, cancellation states and
  `InvocationResult` contract are unchanged.
- Nothing is wired into `Runtime`; no Blender operation is reachable.

## Limits — what this does not provide

- **Publisher identity.** No signatures, no keys, no revocation.
- **Strict fairness.** Bounded wait, round-robin order, no weighting, no SLA.
- **Preemption.** A synchronous capability still cannot be stopped.
- **Fairness across hosts.** Every counter and queue is per process.
- **A cost model.** `cost` is a declared weight; `rate` is a frequency bound;
  neither measures real resource consumption.
- **Protection from a hostile host.** See the threat model's classification.
