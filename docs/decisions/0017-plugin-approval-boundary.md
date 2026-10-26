# ADR 0017 — Approval is a decision, and it is not the hash

- **Status:** accepted
- **Date:** 2026-10-24
- **Phase:** 2.6 (authorisation boundary and install governance), revised in
  2.6.2 after the authorisation-consistency review, again in 2.6.3 after the
  lifecycle-integrity pass, again in 2.6.4 after the read-only governance
  review, and again in 2.6.6 after the 2.6.5 audit; builds on 2.2–2.5
- **Supersedes nothing.** Extends ADR 0014 (isolation), ADR 0015 (execution
  governance) and ADR 0016 (provenance and fairness).

> **Revised in Phase 2.6.2.** A reproduce-and-fix pass over the 13 findings of
> the 2.6.1 review changed four things this ADR used to state loosely. They are
> folded into the decisions below and collected in
> [§ Revisions in 2.6.2](#revisions-in-262): `approve()` now reconciles the
> granted set instead of appending to it; a same-bytes `install()` writes
> nothing; a grant alone no longer authorises a call; and revocation has a
> defined linearisation point.
>
> **Revised again in Phase 2.6.3.** The 2.6.2 audit confirmed six more findings,
> all now fixed and listed in [§ Revisions in 2.6.3](#revisions-in-263). The one
> that changes a statement in this ADR is the concurrency claim: the shared lock
> described in 2.6.2 never existed. What *is* shared between lifecycles in one
> process is the revocation barrier, and across processes neither exists.
>
> **Revised again in Phase 2.6.4.** A read-only review of store identity, the
> revocation generation and the load path found seven more, listed in
> [§ Revisions in 2.6.4](#revisions-in-264). Two of them change what this ADR may
> claim rather than how it behaves: **load** is no longer a place where
> authorisation is decided (it needs the snapshot `activate()` took), and the
> "one process per store" sentence is now enforced by an advisory file lock
> rather than stated. The across-process row of the paragraph above is therefore
> narrower: a second governed process is refused, and a writer that ignores the
> lock is still not stopped.
>
> **Revised again in Phase 2.6.6.** The 2.6.5 audit tested the 2.6.4 fixes as
> claims and found five exception paths nobody had walked, listed in
> [§ Revisions in 2.6.6](#revisions-in-266). One of them was in the *safety*
> mechanism itself: the withdrawal mark could be cleared by a withdrawal that
> failed, so the guard it was meant to stop admitted a call. The mark is owned by
> a token now. The other four are the same shape in different places — a release
> that released more than it owned (the lock without the authority, the pin before
> the decision, the guard before its owner existed), and one error code that told
> callers to retry something that would never clear.

## Context

Phase 2.5 closed the question "are these the bytes?" and left a larger one
untouched: **may this plugin have this capability, and who said so?**

An audit of the actual code — not the documentation — found that the answer was
"whoever wrote the manifest":

1. `PermissionManager.check()` auto-allows `RiskLevel.READ_ONLY`. That is correct
   policy for adapters (reading a public metadata field needs no prompt) and
   catastrophic for plugins: a manifest declaring a read-only capability received
   `Decision.ALLOW` with no human involvement. The manifest authorised itself.
   Measured: `test_a_read_only_capability_cannot_authorise_itself` asserts the
   capability body ran **zero** times.
2. `WasmPluginHost.load()` had no notion of approval at all. Any module that
   matched a pin would be instantiated, so "installed" and "permitted" were the
   same word.
3. There was no revocation path. Deleting a SQLite grant was possible by hand,
   but nothing discarded the in-memory one-shot allowance — so a user who revoked
   everything could still get exactly one more call.
4. `PermissionResult` carried `decision` but not *why*. `ALLOW` from a stored
   grant, `ALLOW` from a policy default and `ALLOW` from a one-shot confirmation
   were indistinguishable to every caller.

## Decision 1 — three sets, three writers, no implication between them

| Set | Written by | Means |
| --- | --- | --- |
| `requested` | the plugin author (manifest) | a claim |
| `approved` | a person, via `approve()` | what that person agreed to |
| `granted` | `PermissionManager` rows | what dispatch honours |

`install()` writes `requested`. `approve()` writes `approved` and then
**reconciles** the grants against it. Nothing else writes a grant, and in
particular:

- installing does not approve;
- a matching hash does not approve;
- a verified ABI version does not approve;
- loading successfully does not approve.

"Reconcile" rather than "create from it" is the 2.6.2 correction. 2.6 answered
`approve()` as *append*: it wrote the new approval and then granted every
capability in it. A second approval that named a **smaller** set therefore left
the capabilities the human had just dropped still granted and still callable —
the record said `{A}` while dispatch honoured `{A, B}`. Measured on the 2.6 code:
approving `{VALID, SECOND}` and then `{VALID}` left `granted == (VALID, SECOND)`,
so the record and the grants disagreed; nothing on the 2.6 call path refused the
dropped capability, because the only check it made was against the grant that
was still there.

`approve()` now takes the difference in both directions and, in order:

1. revokes the grants that are in `granted` but not in the new approval;
2. writes the approval record;
3. creates the grants that are in the new approval but not in `granted`.

The dropped capability is verified by execution, not by reading a table: the
test counts the capability body's runs and asserts zero after the narrowing.

The two fixes overlap, and that was measured rather than assumed. Reintroducing
only the append behaviour on the 2.6.2 tree leaves `approved=(VALID,)` next to
`granted=(VALID, SECOND)` — the disagreement is back — but the call is still
refused, now by the per-call approval guard (`decision="not_approved"`,
`runs == 0`) instead of by the missing grant. Either fix alone stops the
capability; neither is load-bearing *only* because the other exists. On the 2.6
tree, where the grant was the only check on the call path, that same disagreement
was the whole failure.

Options considered for where approval should live:

| Option | Why not chosen |
| --- | --- |
| A boolean on the manifest | the plugin author would set it |
| A field in the install record (status quo) | collapsed content identity with authority |
| A row in the permission table only | cannot say *what the user was shown*, so an upgrade silently inherits |
| **A separate approval record bound to one build (chosen)** | survives reinstall as `previous_approval`, names the capability set, is diffable |

`approve()` refuses `not_requested` (an approval may narrow a request, never
invent one) and `unknown` (a capability this host does not offer). A subset
approval is the normal case, and the test asserts the unapproved capability is not
granted — not merely not approved.

## Decision 2 — provenance goes in the contract, not in the plugin host

The cheap fix was to special-case read-only risk inside `HostCapabilityRegistry`.
That would have worked and would have been wrong: the plugin host would have been
asserting something about `PermissionManager`'s internals, and the next policy
change would silently reopen the hole.

**Chosen:** add `via: DecisionVia | None` to `PermissionResult`, and require
`via ∈ {GRANT, ONE_SHOT}` at plugin dispatch.

| Value | Produced by | Authorises a plugin call |
| --- | --- | --- |
| `GRANT` | stored grant for this principal | yes |
| `ONE_SHOT` | a confirmation, consumed on use | yes |
| `POLICY` | default policy (read-only auto-allow) | **no** |
| `DENY` | refusal | no |
| `CONFIRMATION` | accepted prompt | no — the grant it implies must exist |

Consequences:

- adapter policy is **unchanged** — read-only is still auto-allowed there;
- `PermissionManager` now states provenance on every result it produces;
- a third-party checker that leaves `via` unset is refused, with `"unnamed"` in
  the reason. Defaulting an unknown provenance to "trusted" would have recreated
  the hole for anyone who implements the protocol later;
- this is an additive contract change: `via` is optional, so existing
  constructions still type-check.

## Decision 3 — the guard runs before instantiation, and the lifecycle owns the host

`WasmPluginHost(..., activation_guard=...)` is called after hashing and before
the engine constructs anything:

```
read once → hash → guard → parse → instantiate
```

Instantiating first would run `start` sections and data initialisers of an
unapproved module. "Loaded" must keep meaning "verified and permitted".

`PluginLifecycle` constructs its own host rather than accepting one. A lifecycle
that could be handed a host whose guard was forgotten is a configuration mistake
worth making impossible, not documenting. Tests that do not need a lifecycle keep
constructing `WasmPluginHost` directly, unchanged.

## Decision 4 — re-pinning costs the approval, and revocation reports instead of claiming

**Upgrades.** `install(replace=True)` is legal from `installed`, `approved` and
`active`, and always writes `approval=None`. Refusing to upgrade while active
would push operators to uninstall first, deleting the record they want to compare
against.

**Reinstalling the same bytes is not an upgrade.** `install()` returns
`unchanged` when the module hashes to the pinned value, and until 2.6.2 it did so
after a full write path — rewriting the record, deleting the approval and
recreating the grants. Reinstalling identical bytes reported "no change" to the
caller while quietly discarding the human decision behind it, so a packaging
script that re-installed on every build would silently revoke an approval it
never mentioned.

`install()` now compares the existing record with the incoming one field by
field — state, version, interface version, hash, module name, capability tuple
and manifest path — and returns early without touching the store when they all
agree. The test compares the store **file** byte for byte before and after, and
separately proves the short-circuit is not over-wide: changing the module bytes,
and changing only the requested capability set, both take the full path.

The earlier decision is kept as `previous_approval` so the refusal can be
specific. Without it, a reinstall degrades every subsequent error to "no approval
on file", which hides the difference between *you never approved this* and *you
approved 1.0 and 1.1 adds `reference.write`*. The guard reports
`requires_reapproval=True` with the actual `added`/`dropped` sets.

**Revocation.** `revoke()` deletes the grants, discards pending one-shots, drains
the executor queue, and returns:

```
RevocationReport(revoked_grants=(...), was_active=True,
                 queued_stopped=2, running=1, still_running=1, stopped=False)
```

The three counts are deliberately distinct. 2.6 reported only `still_running`,
which conflated *work that had not started* with *work that was already inside
the module*. A caller reading `still_running=1` could not tell whether one
capability was mid-flight or one request was sitting in the queue — and the
right response differs, because queued work is refusable and in-flight work is
not.

| Field | Means | Can it be stopped? |
| --- | --- | --- |
| `queued_stopped` | waiters removed from the queue and woken with `revoked` | already stopped |
| `running` | calls executing inside the module at the linearisation point | no |
| `still_running` | those of `running` that are still running when the report is built | no |

`still_running` and `stopped=False` remain the honest half. A synchronous
capability runs on a blocking worker to completion; a timeout ends the wait, not
the work. The integration test asserts the in-flight call **still completes**
after revocation, so no future reader can mistake the report for a kill.

**Linearisation point.** Revocation is now defined against *admission*, not
against the queue. `HostExecutor.revoke_plugin()` removes the waiters and marks
the plugin revoked under one lock, and the admission check refuses a revoked
plugin both when a call is first submitted and after it has waited in the queue.
Without the second check a request that entered the queue a microsecond before
the withdrawal would still have run afterwards. The test uses an event to hold a
call inside the capability, submits a second call that must queue behind it, then
revokes and asserts the queued call never ran — no `sleep` is used anywhere in
the suite to make a race come out.

2.6.3 added the part 2.6.2 missed: the block set is a `RevocationBarrier` shared
by every executor serving the same approval store, so a revocation committed
through one lifecycle also refuses a call queued in a *different* host over the
same store. The guarantee is process-local; two processes still keep separate
barriers, and that limitation is documented rather than glossed over.

**Ordering.** 2.6 wrote the grants before the record in both `approve()` and
`revoke()`, and stated the invariant as "the record is written last". That was
the right invariant for *granting* and the wrong one for *revoking*: it meant a
revocation whose grant deletion failed could still fall through to writing a
record. 2.6.2 makes the ordering depend on which direction can do damage.

| Operation | Order | What a failure leaves behind |
| --- | --- | --- |
| `approve()` (narrowing) | revoke stale → write record → grant missing | stale revoke fails: `approval_saved=False`, record untouched, old set fully intact |
| `approve()` (widening) | — → write record → grant missing | grant fails: `approval_saved=True`, `ungranted=[...]`, and nothing claims it was granted |
| `revoke()` | write record → drain executor → delete grants | record write fails: `revocation_persisted=False`, and **nothing else is attempted** |

The rule behind the table: never persist a statement that the new state is in
force before the work that makes it true has succeeded. For `approve()` that is
the grant creation; for `revoke()` it is the record, because the record is what
later reads treat as authority. When a revocation cannot be recorded, the plugin
stays approved, its grants stay in place, its executor is not blocked and a call
still runs — the failure is loud and the state is exactly what it was.

`JsonApprovalStore` uses `mkstemp` + `fsync` + `os.replace`. A corrupt file raises
`plugin_lifecycle` instead of parsing as "no approvals", because silently losing
every approval is indistinguishable from a fresh install and would invite
re-approving without noticing.

## Decision 5 — the CLI is read-only until the boundary is designed

`scripts/plugin_admin.py` has `list` and `inspect`, and a test asserts no
state-changing verb exists.

The reason is not laziness. A CLI that could `approve` would be a second path to
authority: it runs as whichever OS user invoked it, records no human decision,
and has no designed relationship with the running Runtime's permission store.
Adding a mutating verb before that boundary is designed would create exactly the
bypass this phase exists to close.

`inspect` re-reads the pin with `InstallRecord.from_dict` rather than
`verify_install`, because a verifier that raises on drift cannot *report* drift.
Its job is to print `MODIFIED SINCE INSTALL` and exit 3, not to refuse to look.

## Decision 6 — a grant is not an approval, and dispatch asks the record

Decisions 1–5 bound the *activation* of a plugin to an approval. They did not
bind an *individual call* to one, and 2.6.1 showed the gap with a working
reproduction: write a grant row into the SQLite store by hand, and the
capability body runs. The permission layer answered "allowed" honestly — a grant
is a grant — but nothing on the call path asked whether a person had approved
that capability for that build.

A second, quieter version of the same hole: an approval withdrawn while a call
was queued did not stop the call, because admission only checked the permission
layer at submission time.

Both are closed the same way. `PluginLifecycle` installs a per-call
`dispatch_guard` on the host registry which, under one lock, asks: **does a
current approval cover this capability, for this build, right now?**

```
plugin_id, capability
  → is a revocation in progress?          → refuse (not_approved)
  → re-read the record from disk
  → state ∈ {approved, active}?           → refuse
  → approval present?                     → refuse
  → approval hash/version/iface match?    → refuse
  → capability ∈ approval.capabilities?   → refuse
  → otherwise: lift the executor block and allow
```

The refused call is `DENIED_NO_GRANT` with `decision="not_approved"`, a third
distinct decision string alongside `policy_default` and `revoked`; the audit
record says which of the four refusals fired rather than a shared "denied".

The grant remains necessary — the guard does not replace the permission check,
it adds a question the permission check cannot answer. A grant means "this
principal holds this capability"; an approval means "a person decided this build
should". Removing either one would restore a hole this phase exists to close,
which is why the guard was added rather than the provenance check relaxed.

## Revisions in 2.6.2

Collected, so a reader who knew 2.6 can see what moved:

| Was | Now | Why |
| --- | --- | --- |
| `approve()` appended to the granted set | reconciles: revoke stale, write, grant missing | P0: narrowing an approval left the dropped capability callable |
| `install()` on unchanged bytes rewrote the record | returns early, store file untouched | a reinstall silently discarded a human decision |
| a grant authorised a call | a grant **and** a current approval do | a hand-written grant row ran code |
| revocation checked the permission layer at submission | admission refuses revoked, at submit *and* after queueing | queued work ran after withdrawal |
| `still_running` only | `queued_stopped` / `running` / `still_running` | the report could not distinguish queued from in-flight |
| record loaded once, cached | re-read on every transition and every guarded call | `mtime` is not proof of content |
| `uninstall()` deleted the record | leaves a tombstone | a deletion and a fresh install were indistinguishable |
| `state` optional on the audit record | required, and written `refused` on every refusal path | an audit row that cannot say how a call ended is not an audit |
| a pinning host without a guard was legal | raises unless `allow_unguarded=True` | "checks bytes" and "checks permission" must not look alike |

## Revisions in 2.6.3

The 2.6.2 audit confirmed six findings. None of them is a new feature; each one is
a place where the code said something the state could not support.

| Was | Now | Why |
| --- | --- | --- |
| `install()` compared the manifest path as a stored string | compares canonical paths (`resolve()` + `normcase`), and takes the conservative full path when a path cannot be canonicalised | a relative spelling looked like a new install and cleared a live approval while reporting `unchanged` |
| the record's ABI version was compared against the pin | compared against the manifest on disk, and written from it | the pin's own `unchanged` test does not carry `interface_version`, so the two stale copies always agreed |
| a guard defect escaped `dispatch` and was audited `admitted=True executed=True state=failed` | audited `decision="not_approved" state="refused"`, exception type recorded, message logged only | the capability ran zero times while the audit said it ran |
| a refused call left its one-shot parked in `_pending` | one idempotent `settle()` covers every exit: return if nothing started, spend if it did | the user's "yes, once" was silently consumed by a refusal |
| the revocation block was per `HostExecutor` | one `RevocationBarrier` per canonical store path, shared by every executor serving it | a call queued in a second host on one store ran after the first host revoked |
| an expired grant counted as current authority | `list_effective_grants()` is the reconciliation input; `list_grants()` stays as audit history | `inspect` showed the capability as granted while dispatch refused it |
| a store failure escaped `approve()` as a bare `OSError` | `plugin_lifecycle` with `approval_saved=False`, `revoked_stale=[...]` and the original exception as `__cause__` | the caller could not tell "nothing happened" from "the narrowing already ran" |

The 2.6.3 review then probed the fixes instead of trusting them, and three more
gaps in the same area were real. They are recorded here because two of them
changed a *guarantee* rather than a message:

| Was | Now | Why |
| --- | --- | --- |
| the guard lifted the block unconditionally after reading the record | captures the barrier's generation before the read, lifts with `lift_if_unchanged`, and refuses `plugin_not_approved` if a revocation landed in between | a revocation that landed during the read was discarded by the very check that had just authorised the call |
| a shared `MemoryApprovalStore` still gave each lifecycle a private barrier | barriers for pathless stores are keyed by the store object (`WeakKeyDictionary`) | the row above it ("one barrier per store") was true only for stores with a path, so in-memory tests proved less than they appeared to |
| get-or-create was unguarded, and an unresolvable path fell back to a private barrier | the lookup runs under a `threading.Lock`, and an unresolvable path raises `plugin_lifecycle` | two threads could each build a barrier for one store, and a silent downgrade is indistinguishable from sharing until a revocation fails to cross |

Two statements in this ADR changed as a result, and both were claims rather than
implementations: the "shared lock" in the 2.6.2 section (there is no shared
transition lock — see plugin-lifecycle.md §8.1) and the scope of the revocation
guarantee (one process; two processes on one store are still not covered).

## Revisions in 2.6.4

Phase 2.6.4 started as a read-only review of four questions: the revocation
generation and every execution entry; store identity and the barrier registry's
lifetime; whether the manifest, pin, approval and loaded module are bound to one
another; and whether "one process per store" was enforced anywhere except in
prose. Seven findings, all reproduced first. None adds a feature.

| Was | Now | Why |
| --- | --- | --- |
| a store constructed with a relative path kept that string, so a `chdir` produced a second barrier for one file | the path is resolved once at construction; that value is the barrier key, the hard-link check and the lock name | one lifecycle recorded `revoked` while another over the same file answered `is_revoked=False` and ran a queued call (`runs=2`) |
| a hard link to the store was `os.samefile`-identical but got its own barrier | construction refuses a store with `st_nlink > 1` | "same file, two names" is exactly the identity the barrier cannot express |
| a pathless store that could not be weakly referenced fell into `except TypeError` and silently got a private barrier | the `TypeError` is a `plugin_lifecycle` refusal | two lifecycles over the same object disagreed about a block; the path branch was already fail-closed, so this was an inconsistency with a security consequence |
| a shared executor adopted a second store's barrier and overwrote the first | an executor is bound to one store; a second `adopt_barrier` raises, and withdrawal marks carry across on a legitimate switch | one `revoke()` left store A unblocked and store B blocked (measured `A=False`, `B=True`) |
| `uninstall()` blocked admission and then wrote the record; another lifecycle's guard read the still-`approved` record, admitted the call, and lifted the block | a shared withdrawal mark on the barrier (`begin_withdrawal` / `end_withdrawal` / `withdrawing`), checked by both lift paths | the block went `True → False` and the call ran — the flag has to be visible *across instances*, not only to the one revoking |
| `host.load()` judged the host's cached record | a guarded host refuses to load without the approval snapshot, which only `activate()` obtains after refreshing the store | a build revoked by another lifecycle was still instantiated (`LOADED digest=b0dcb207035d`) while `activate()` refused it |
| `activate()` read the manifest twice | one manifest object for the whole activation; `load()` receives the parsed object and `activate()` refuses if the returned plugin is not that object | a swap in between could mark build X `active` while build Y was instantiated |
| "one process per store" was documentation | an exclusive advisory lock on `<store>.lock`, taken before the host is built; `plugin_store_locked` when busy, `plugin_lifecycle` when it cannot be established; no no-op fallback | a second process kept its own barrier and ran a call after the first process revoked (`runs=2`) |

One consequence deserves its own line, because it is the kind of claim this ADR
exists to avoid: the lock is **advisory**. It stops a second *governed* process,
not a writer that opens the JSON without the library. The guarantee is now
"two governed processes cannot coexist", not "the store has one writer".

## Revisions in 2.6.6

Phase 2.6.6 is the hardening pass that followed the 2.6.5 audit. It changes no
interface a caller had to satisfy and adds no feature: every row below closes a
path on which a 2.6.4 guard answered *allow* when it should have refused, or
released something it did not own.

| Was | Now | Why |
| --- | --- | --- |
| the withdrawal mark was a set with an unconditional `discard`, so any withdrawal could clear any other's mark | `begin_withdrawal` returns a `Withdrawal` token and `end_withdrawal(token)` releases only that token; a foreign, repeated or cross-barrier token releases nothing; `adopt_barrier` carries blocks but never marks | an uninstall parked in its store write + a revoke whose write failed = the guard admitted a call mid-uninstall (measured; the generation check agreed with the failure because a guard captures it afterwards) |
| `close()` released the lock but left the instance able to write | a permanent `_closed` state; six writing entry points plus the activation and dispatch guards refuse with `plugin_lifecycle` | a closed lifecycle wrote the store while another process held the lock (measured: `approve` succeeded, record changed) |
| `uninstall()` removed grants and the pin before writing the record | record → grants → pin; a failed record write leaves everything intact and raises a structured error with the `OSError` chained; a stuck pin is logged, not swallowed | a failed write left the pin gone under a record that still said `approved`, and the raw `OSError` escaped |
| `registry.dispatch_guard` was installed before the lock and the host | installed last, and a failed construction releases the lock it took | a refused construction left a guard bound to a lifecycle with no `_host`, so dispatch raised `AttributeError` |
| a lock file that could not be created was reported as `plugin_store_locked` | only real contention keeps that code; create/open failures are `plugin_lifecycle` with the cause chained | "another process holds this" is a retryable condition; a read-only directory is not |

The ten mutants that pin these are in `scripts/wasm_guard_mutation_check.py`, and
the harness now also runs the three 2.6.4 security suites, which it previously
did not: 51 mutations, 51 caught, 0 survived, 0 skipped at the end of that phase.

## Revisions in 2.6.7b

Phase 2.6.7 audited crash consistency and lifecycle races read-only; 2.6.7a
formalised the two races as failing tests; 2.6.7b fixes them. Both are cases where
a *correct* decision was undone by what happened around it — one by a lock that
was released too early, one by a `unlink` that trusted a path instead of a file.

| Was | Now | Why |
| --- | --- | --- |
| `close()` released the store lock immediately, while an operation it had already admitted was parked inside an `await` and would keep writing afterwards | every writing entry point is counted (`@_in_flight`); `close()` sets `_closed` and releases only when the count is zero, the last operation out releases otherwise; `aclose()` waits for the count to drain and then closes the host | measured in 2.6.7a with a real second process: after `close()`, `probe()` acquired the store and read it while an uninstall was still writing. "Refuses new work" was implemented, "finished with the old work" was assumed |
| `aclose()` was `close()` under an async signature: it neither waited nor closed the `WasmPluginHost` it owns | `aclose()` = `close()` + `await _drained` + `host.aclose()`, returning the host's leftover count; called from inside one of its own operations it refuses with `details["lifecycle"] == "closing_self"` | a shutdown that returns while work is still running is indistinguishable from a clean one; a task waiting on itself hangs with no traceback |
| `uninstall()` deleted whatever file was at the pin path | the pin is deleted only if the file itself still says `(plugin_id, wasm_sha256)` is this install; a replaced pin, a symlink, an unreadable file or a directory is **kept** with a warning naming what was found | 2.6.7a: a replacement pin was deleted in both runs — with and without `close()` — so this was a defect of its own, not a symptom of the lock race. The store lock does not stop a non-cooperating writer, so the file has to be checked |
| re-running an uninstall that had already recorded `uninstalled` raised `cannot move … uninstalled to uninstalled` | `uninstalled → uninstalled` is a legal cleanup edge; approval and capabilities are already empty, so the retry cannot authorise anything and only retries the two removals | a first attempt can fail *after* the tombstone was written (grants store down, pin kept as a stranger), and the only other route back — `install` then `uninstall` — needs the module still on disk |

Five mutants pin these, and the harness now also runs the two suites 2.6.7b added:
56 mutations, 56 caught, 0 survived, 0 skipped over 29 test targets. One existing
mutant (`closed-lifecycle-still-writes`) was re-pointed because `close()` no longer
has the early return it targeted — a zero-match anchor fails the run as `SKIPPED`
rather than being quietly dropped.

## Revisions in 2.6.8b

Phase 2.6.8 audited the *revocation* side of the same question read-only: what
happens to the state machine, the grants and the block after a revocation fails
part-way. 2.6.8a wrote the three gaps as tests and left them red; 2.6.8b makes
them pass. All three are about the same asymmetry — the decision is persisted
before the cleanup, so the cleanup has to be recoverable.

| Was | Now | Why |
| --- | --- | --- |
| a revocation that recorded `revoked` and then failed at the grants step could not be retried: `revoke()` again raised `cannot move … revoked to revoked` | `revoked → revoked` is a legal **cleanup** edge. The retry re-writes the same decision, drains the queue, withdraws the residual grants, and does none of: create a grant, change the record, lift the block | the alternative routes are `install()` (does not touch grants) and `uninstall()` (deletes the pin), so the failure would be unrecoverable by the verb that failed — measured in 2.6.8 with a real `PermissionManager` and a real SQLite row left behind |
| the same failure escaped as whatever the authority raised (`RuntimeError`, `sqlite3.OperationalError`), after the record had already been written | `plugin_lifecycle` with `revocation_persisted=True` / `grants_withdrawn=False` and the original chained — the pair `uninstall()` already used for the same two facts | the one case a caller must tell apart from "nothing happened" was the one case with no classification on it; both halves are fail-closed either way, so this is about being able to say which half |
| `approve(plugin, [])` recorded an approval with no capabilities **and** lifted the post-revocation block, making a revoked plugin admissible again | the empty approval is still accepted and recorded (no new public refusal was invented), but it does not lift the block; the plugin stays refused, now as `not approved` rather than `revoked` | a decision that authorises no capability cannot restore admissibility a revocation took away. Rejecting the call outright was the other candidate and was declined: no document in this repository defines an empty approval as illegal, so making it an error would be a new contract rather than a fix |

Three mutants pin these, one per contract: `revocation-cannot-be-retried`,
`revoke-grants-failure-escapes`, `empty-approval-lifts-the-block`. The harness is
now 59 mutations, 59 caught, 0 survived, 0 skipped over 30 test targets, and each
new mutant is attributed to the single test that kills it.

Deliberately **not** changed in this phase, and recorded instead: `install()` on a
`revoked` record still returns successfully while writing nothing; `aclose()` has
no timeout; and what `host.aclose()` does after it raises was unverified. All three
went into `plugin-lifecycle.md` §12 rather than being silently folded into this
fix. The third was taken up in 2.6.9b (below); the other two are still open.

## Revisions in 2.6.9b

Phase 2.6.9a audited `close()`/`aclose()` read-only and measured two defects; 2.6.9b
writes them as failing tests first, then fixes exactly those two. Neither changes
what `close()` promises — one is about *where* a wake-up is scheduled, the other
about *how* a failure is reported.

| Was | Now | Why |
| --- | --- | --- |
| `aclose()` waited on an `asyncio.Event` that the last in-flight operation `set()` from whatever thread it finished on. Awaiting from a **different** event loop never woke: the callback was appended to that loop without waking its selector | the wait is a set of futures, one per `aclose()` call, each created on its caller's loop; the last operation out wakes each with `loop.call_soon_threadsafe`, and cancels a waiter whose loop is already gone | measured in 2.6.9a: `aclose()` still pending after 8 s with `inflight=0`, the store lock **already released** and `host_close_calls=0` — a second process could take the store while the shutdown never returned. Every existing test ran a single loop, so nothing in the suite could see it. Isolation probe: `event.set()` from another thread → `woken=False`; `loop_b.call_soon_threadsafe(event.set)` → `woken=True` |
| `host.aclose()` failing escaped as whatever the host raised (`RuntimeError`), after `close()` had already released the store lock — indistinguishable from "the store is still locked" | `plugin_lifecycle` with `host_closed=False` and the original chained; retryable, because `HostExecutor.aclose()` is re-entrant and records no "already attempted" flag | the caller's decision after a shutdown failure depends on *which* half was reached. `host_closed=False` reports that shutdown did not succeed, not that the host is running — no closed state is exposed to ask (2.6.9a, I5) |

`host_closed=False` is deliberately weaker than "the host is open": it is the
outcome of the call, because that is the only thing the code can observe. That
limit is stated in `plugin-lifecycle.md` §12 rather than papered over.

Two mutants pin these (`drain-wakeup-ignores-the-waiter-loop`,
`aclose-host-failure-escapes`), and one existing mutant was re-pointed because
`aclose()` no longer contains the line it targeted; the harness is now 61
mutations, 61 caught, 0 survived, 0 skipped over 31 test targets.

Deliberately **not** done here, and recorded instead: a timeout on `aclose()`; a
"call the host once" deduplication for repeated `aclose()` calls (today every call
reaches `host.aclose()`, which is harmless only because that method happens to be
re-entrant); and any change to what a cancellation at the host-close point reports.
Those are contract decisions, not repairs, and folding them into a reliability fix
would decide them by accident.

## Consequences

- A plugin can no longer obtain any capability without an approval that names it,
  is bound to one build, and can be withdrawn.
- An approval is now a **set**, not an accumulating list: what the record says is
  approved and what dispatch honours are reconciled on every `approve()`.
- Two error codes (`plugin_not_approved`, `plugin_lifecycle`) and one contract
  field (`DecisionVia`) are added; no status code, ABI or `InvocationResult`
  field changed. 2.6.2 adds one lifecycle keyword (`allow_unguarded`), two
  `RevocationReport` fields and one required `HostCallOutcome` field, and
  changes no signature that a caller already had to satisfy. 2.6.4 adds a third
  error code (`plugin_store_locked`), one lifecycle method pair (`close()` /
  `aclose()`, because the store lock is an OS resource), and one **required**
  keyword on the guarded load path (`approval=`), which is a real signature
  change — made on purpose, so that no caller can load by forgetting to authorise.
- `PermissionManager.revoke()` is new, and clears the in-memory one-shot set and
  any pending one-shot reservation as well as the SQLite rows.
- Nothing is wired into `Runtime`; `runtime/pyproject.toml` still does not depend
  on `deskweave-plugin-wasm`. A runtime-side plugin loader remains unbuilt.

## Limits — what this does not provide

- **Publisher identity.** An approval is a local record of a local decision, in
  the same store as the grants under the same OS user. Anyone who can rewrite the
  store can approve anything. No signatures, no keys, no trust root.
- **Preemption.** `stopped=False` is not a bug report; it is the design. Queued
  work is refused and removed, work already inside the module is not.
- **Durability across a power cut.** `JsonApprovalStore` fsyncs the file before
  `os.replace` but does not fsync the containing directory, because Python has no
  portable way to open and fsync a directory. On a crash between the two, the old
  record can survive the new one. Authoring an approval is not a
  once-per-millisecond operation, so the cost of a portable directory fsync was
  not worth the complexity — but the gap is real and is recorded, not denied.
- **One *governed* process per store (2.6.4), and it is advisory.** The transition
  lock is per lifecycle instance, not per store; two instances in one process each
  serialise their own transitions, the record write is atomic, and the test
  asserts the invariant that survives either order (`approved == granted`). What
  those instances do share is the revocation barrier, keyed by resolved store
  path. Since 2.6.4 a second *process* over the same store is refused at
  construction by an exclusive lock on `<store>.lock` — `plugin_store_locked`,
  structured, no no-op fallback — so the configuration the barrier cannot cover
  no longer exists. What that does **not** cover: a writer that opens the JSON
  without the library (an older build, a script, a hand edit) is not stopped, and
  the store is still not tamper-evident. Since 2.6.6 the boundary is stated in
  full: the lock is taken by `PluginLifecycle` and by nothing else, so a bare
  `WasmPluginHost(activation_guard=...)` built without a lifecycle is a governed
  *host*, not a governed *store*, and the single-writer rule does not reach it.
  Closing that would make the lock a property of the store object rather than of
  the lifecycle — a separate change, recorded here rather than implied.
- **Fairness.** Unchanged from ADR 0016; `revoke_plugin()` removes waiters and
  marks the plugin revoked, it does not alter the round-robin order.
- **A permissions UI.** `approve()` is an API. The surface that lists requested
  capabilities and asks a human is not built.
- **Defence against a hostile host process.** Out of scope; see the threat model.
