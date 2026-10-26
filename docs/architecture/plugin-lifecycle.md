# The install lifecycle: who decided, may it run, what is true now

Phase 2.5 answered a question about bytes: *are these the bytes somebody pinned?*
That answer is necessary and completely insufficient. A correctly hashed plugin
that nobody approved is still an unapproved plugin, and the hash has nothing to
say about it.

This document is about the three questions that are not about bytes:

| Question | Answered by | Answer lives in |
| --- | --- | --- |
| Who decided this plugin may ask for these capabilities? | `PluginLifecycle.approve` | the approval record |
| May *this* build run, right now? | the activation guard | `PluginNotApprovedError` or nothing |
| May *this capability* run, right now? | the dispatch guard | `decision="not_approved"` or nothing |
| What is actually true at this moment? | `PluginLifecycle.inspect` | a read-only `Inspection` with `approval_in_force` |

Code: `packages/plugin-wasm/src/deskweave_plugin_wasm/lifecycle.py`.

## 1. The three sets, and why they are three

```
requested   the plugin author, in the manifest     a claim, never a grant
approved    a person, via approve()                what that person agreed to
granted     PermissionManager rows                 what dispatch will honour
```

Every authorisation bug in this shape of system comes from collapsing two of
these. The specific collapse that was live in this repository before Phase 2.6 is
worth naming, because it was not a typo:

> `PermissionManager` auto-allows `RiskLevel.READ_ONLY`. A plugin whose manifest
> declared a read-only capability therefore received `Decision.ALLOW` with no
> human involvement at all. The manifest authorised itself.

The fix is at the contract level (`DecisionVia`, §5) and it leaves adapter policy
untouched: read-only capabilities are still auto-allowed for adapters, which is
correct there and was measured to be wrong here.

`install()` writes `requested` and nothing else — and when the bytes on disk are
the ones already pinned, it writes nothing at all (§3.1). `approve()` writes
`approved` and then **reconciles** the grants with it: it adds what the new
decision covers and deletes what it no longer does. There is no code path in
which installing, hashing or verifying a module produces a grant:

| Event | Creates a grant? |
| --- | --- |
| `install()` succeeds | no |
| the declared hash matches the file | no |
| `interface_version` equals `ABI_VERSION` | no |
| the module loads and the ABI probe returns the right version | no |
| `approve(plugin_id, [...])` | **yes**, exactly the approved set — *and deletes the ones it dropped* |

`approve()` refuses three things, all deliberately:

- `not_requested` — approving a capability the manifest never asked for. An
  approval may **narrow** what a plugin asks for; it may never invent.
- `unknown` — a capability this host build does not offer. That is a typo or a
  stale catalogue, and either way it must not become a grant.
- a narrowing decision from an authority that cannot enumerate grants. Without
  the list there is no way to take one away, and an approval that leaves the old
  grant in place is not the decision that was recorded.

A subset approval is legal and is the interesting case: a manifest that requests
three capabilities and gets two is the normal outcome of a person reading the
list. The test
`test_approving_a_subset_grants_only_that_subset` asserts the third capability is
not granted, not merely that it is not "approved".

### 1.1 An approval is a set, not a growing list

Before Phase 2.6.2, `approve()` only ever *added* grants. Approving `{A, B}` and
then deciding `{A}` produced a record that said `approved=(A,)` while dispatch
still honoured `B` — measured, not hypothesised:

```
approve({A, B}) → approve({A})
inspect: approved=('A',)  granted=('A', 'B')
the plugin then executed 'B'  (decision=allow)
```

The fix is to make the granted set a function of the decision every time:

```
current := grants the permission layer would honour for this plugin, read now
stale   := current − wanted      → revoked before anything is written
missing := wanted − current      → granted after the decision is written
```

The order is chosen, not incidental (§8). A narrowed approval is therefore not a
new kind of operation: it is `approve()` doing what it always claimed to do.
`approved → approved` is a legal transition for exactly this reason — revising a
decision that is not yet in use is one decision made twice — while
`active → approved` stays illegal, because an approval must not change under a
plugin that is running. Narrowing an active plugin goes through `revoke()` first.

## 2. The state machine

```
                     discover()
                        │
                   ┌────▼─────┐
                   │discovered│
                   └────┬─────┘
              install() │
                   ┌────▼─────┐   install(replace=True)  ┌──────────┐
                   │ installed├─────────────────────────►│installed │
                   └────┬─────┘  (approval := None)      └──────────┘
             approve()  │
                   ┌────▼─────┐
                   │ approved │◄──────────────┐
                   └────┬─────┘               │ approve()
            activate()  │                     │
                   ┌────▼─────┐               │
                   │  active  ├───────────────┤
                   └────┬─────┘               │
            revoke()    │                     │
                   ┌────▼─────┐  approve()    │
                   │ revoked  ├───────────────┘
                   └────┬─────┘
          uninstall()   │
                   ┌────▼──────┐
                   │uninstalled│──► discovered (re-discover)
                   └───────────┘
```

`approve()` from `approved` back to `approved` is a revised decision (a narrowed
or widened capability set). `install()` of the bytes already pinned leaves the
diagram unchanged: it writes nothing at all (§3.1).

Two self-loops are missing from the drawing because they are not progress through
the lifecycle but **retries of the last step**: `revoked → revoked` (§6.3) and
`uninstalled → uninstalled` (§7.2). Both exist for the same reason — the decision
is persisted before the cleanup, so a cleanup that failed must be re-runnable by
the verb that failed, and neither may change what the record says.

`_TRANSITIONS` is the specification; anything absent from it raises
`plugin_lifecycle` with the current state, the requested state and the set of
legal targets. The unit tests walk **every** edge in the table, including the
ones that must fail.

| From | Legal targets | Why |
| --- | --- | --- |
| `discovered` | `installed`, `uninstalled` | nothing is pinned yet; uninstall is a no-op cleanup |
| `installed` | `approved`, `installed`, `uninstalled` | re-pinning is legal (§3) |
| `approved` | `approved`, `active`, `revoked`, `installed`, `uninstalled` | revise the decision, run, withdraw, re-pin, or remove |
| `active` | `active`, `revoked`, `installed`, `uninstalled` | activating twice is idempotent; `installed` is an upgrade |
| `revoked` | `approved`, `revoked`, `uninstalled` | withdrawing is not permanent — a person may change their mind; `revoked` itself is the retry of a cleanup that failed (§6.3) |
| `uninstalled` | `installed`, `discovered`, `uninstalled` | reinstall, fall back to a bare manifest, or finish a cleanup that failed after the tombstone was written |

Two deliberate choices hide in that table:

**Re-pinning is allowed from `approved` and `active`.** Refusing an upgrade while
a plugin is active would push operators to uninstall first — which deletes the
very record they want to compare against, and makes the audit trail worse. What
the transition *costs* is the approval: `install()` writes `approval=None` when
the build changed. An upgrade is therefore always followed by a fresh decision.
Installing the bytes that are already pinned is not an upgrade at all and writes
nothing (§3.1).

**`approved → approved` is legal, `active → approved` is not.** A decision that
is not yet in use may be revised — that is how an approval is narrowed (§1.1) —
but a plugin that is running must not have its capability set changed underneath
it. Narrowing an active plugin is `revoke()` and then `approve()`.

**Revoked is not terminal.** `revoke → approve` is legal because revocation is a
withdrawal of a decision, not a ban. The plugin is not blacklisted; the person
simply has to decide again, against the current build.

**But approving *nothing* does not undo it.** `approve(plugin, [])` is accepted —
the state machine permits `revoked → approved` and no rule says a capability set
must be non-empty, so rejecting it would be a new public behaviour (Phase 2.6.8b,
C3) — and it writes a record with `capabilities=()`. What it must not do is lift
the post-revocation block: a decision that authorises no capability cannot restore
admissibility a revocation took away. The plugin stays refused, and the reason is
now `not approved` rather than `revoked`. The block is lifted by the first
approval that covers something.

## 3. An upgrade never inherits a decision

`install()` with `replace=True` moves state to `installed` and writes:

```python
record = LifecycleRecord(
    ...,
    approval=None,
    previous_approval=existing.approval if existing is not None else None,
)
```

`previous_approval` exists so the refusal can be *specific*. Without it, a
reinstall would erase the approval and every subsequent activation attempt would
report "no approval on file" — technically fail-closed, practically useless, and
it hides the difference between these two situations:

> You never approved this plugin.
> You approved version 1.0, and version 1.1 adds `reference.write`.

The guard compares the live build against `previous_approval` and raises
`plugin_not_approved` with `requires_reapproval=True` and the actual diff
(`added`, `dropped`). `check_approval()` is the single implementation of that
comparison; the guard and `activate()`'s audit record both call it, so they
cannot drift apart.

### `check_approval` order

| # | Condition | Refusal reason |
| --- | --- | --- |
| 1 | `approval.plugin_id != manifest.plugin_id` | the approval names a different plugin id |
| 2 | `approval.wasm_sha256 != digest` | the module content changed since it was approved |
| 3 | `approval.version != manifest.version` | the plugin version changed |
| 4 | `approval.interface_version != manifest.interface_version` | the plugin's ABI version changed |
| 5 | `added` is non-empty | the manifest now requests capabilities that were not approved |

Digest before version, on purpose: the same version with different bytes is the
more alarming of the two, and it is exactly what an in-place rebuild produces.

Capabilities that were approved and later *dropped* from the manifest are
harmless (narrowing) and are recorded, not refused. Capabilities approved but
never requested are reported as `unused` so the approval surface can be tidied —
also not a refusal.

## 3.1 Installing the same bytes is not a new decision

`integrity.install()` already returned `unchanged` when the module matched the
pin. The lifecycle ignored that and rewrote its record anyway, which cleared a
live approval as a side effect of an operation that changed nothing:

```
approve({A}) → state=approved
install(same bytes) → state=installed, approved=()
```

The plugin was then not activatable, and nothing the user did had asked for that.
Now `install()` short-circuits when **all** of these hold:

| Compared | Against | Why it is in the list |
| --- | --- | --- |
| `integrity.install()` returned `unchanged` | the pin | the bytes and the declared capability set match the pin |
| `version`, `interface_version` | the manifest on disk | the record describes the build that is actually there |
| `wasm_sha256`, `module_name` | the pin | the record points at the same file |
| the recorded capability set | the manifest on disk | the record's `requested` is what a person read |
| `manifest_path` | the same file, canonically | `uninstall` finds the pin from it, and two spellings of one path are one path |
| state is not `discovered` / `uninstalled` | — | those have to be written, or they stay invisible |

Two of those columns changed in Phase 2.6.3, and both were exploitable in the
"the approval disappears" direction:

* `manifest_path` used to be compared as the **stored string**. `/p/a.json` and
  `a.json` (run from `/p`) are one file, and the second spelling took the full
  path — clearing a live approval while `integrity.install` still reported
  `unchanged`. It is now `resolve()` + `normcase`, so case and separator
  differences and symlinks fold together on the platform that has them. A path
  that cannot be canonicalised is treated as **different**, which costs a
  conservative rewrite instead of a wrong claim.
* `interface_version` used to be compared against the **pin**, which does not
  carry it in its own `unchanged` test — so an ABI change under identical bytes
  compared two stale copies of the same fact and agreed with itself. It is now
  read from the manifest on disk, and the record is written from the manifest too.

When it holds, nothing is stored: no record write, no grant churn, no `reinstalled`
event, no `updated_at` bump. `test_reinstalling_the_same_bytes_does_not_touch_the_record`
and `test_the_same_path_and_bytes_leave_the_store_byte_identical` compare the
**entire store file** before and after, so "nothing was written" is a byte-level
statement rather than an intention.

Anything that fails the comparison takes the normal path — a different build, a
different version, a manifest that grew or dropped a capability, an ABI change, a
different directory, or a plugin that was uninstalled. Each of those has its own
test (`test_a_different_build_is_not_short_circuited`,
`test_a_new_capability_set_is_not_short_circuited`, and the
`test_a_changed_*` / `test_a_copy_in_another_directory_is_a_different_installation`
cases in `tests/integration/test_lifecycle_integrity.py`), so the short-circuit
cannot be mistaken for "install never does anything". The reason it did not
short-circuit is stored as `last_detail["record_mismatch"]`, so a cleared approval
can be explained after the fact.

## 4. The guard runs before the engine

`WasmPluginHost(..., activation_guard=callable)` calls the guard after hashing the
bytes and **before the engine instantiates anything**:

```
read bytes once → hash → activation_guard(manifest, digest, snapshot) → parse → instantiate
```

The placement is the point. A build nobody approved must not be constructed at
all, so that "loaded" continues to mean "verified and permitted". Instantiating
first and checking afterwards would run `start` sections and data initialisers of
an unapproved module.

**Loading is not authorising**, and Phase 2.6.4 made the entry say so. The
snapshot the guard judges is a required argument: `load()` refuses outright
(`plugin_not_approved`, `state="not_approved"`) when a host with a guard is asked
to load without one. It used to answer that question from the host's own cache,
so a build revoked by another lifecycle was still instantiated — measured:
`LOADED digest=b0dcb207035d` while the record said `revoked`, and `activate()`
correctly refused the same plugin a moment later. The only way to obtain a
snapshot is to refresh the approval store, which is what `activate()` does under
its lock; a bare `WasmPluginHost` without a guard is unchanged and still loads.

`activate()` takes **one** manifest object for the whole transition — the record
it looks up, the manifest the guard judges, the digest of the bytes that run, and
the record it writes are all derived from that snapshot. Previously it read the
manifest twice, so a swap in between could mark one build `active` while another
was instantiated. Two things now stand in the way: `load()` is handed the parsed
manifest instead of a path, and `activate()` refuses (`plugin_lifecycle`) if the
plugin it gets back is not that same object.
`test_a_swap_between_the_phases_cannot_change_what_was_verified` swaps the file
mid-activation to a *pin-equivalent* manifest — same version, same bytes, same
capabilities, so neither the pin nor the approval can be what notices — and
asserts the activation completed against the object it verified;
`test_a_version_swap_between_the_phases_is_refused` swaps to a different version
and asserts the refusal, and that nothing was registered on the host;
`test_the_approval_check_alone_refuses_a_changed_version` shows the approval
layer reaches the same verdict in a lifecycle without a pin.

`PluginLifecycle` constructs its own host for the same reason. A lifecycle
instance that accepted a caller-supplied host could exist beside a host whose
guard was forgotten; that is not a configuration mistake worth allowing.

A host that pins bytes but never asks who approved them is the same mistake in a
different shape: it *looks* strict, and it grants any plugin that matches its own
pin the right to run. `WasmPluginHost(require_install_record=True)` therefore
raises unless it is given an `activation_guard`, and the escape hatch has to be
spelled out:

```python
WasmPluginHost(require_install_record=True)  # plugin_lifecycle
WasmPluginHost(require_install_record=True, allow_unguarded=True)  # integrity only
```

`allow_unguarded=True` is what the integrity layer's own tests use, and it is
deliberately explicit: the configuration is legitimate, but it must not be reached
by forgetting a keyword argument.

`activate()` reports sequencing errors separately from authorisation errors:

| Record state | Error |
| --- | --- |
| `discovered`, `uninstalled` | `plugin_lifecycle` — there is nothing installed to activate |
| anything else | whatever the guard raises: `plugin_not_approved` with the reason |

The distinction matters to the person reading the error: "not approved" tells
them what to do next; "illegal edge from uninstalled to active" does not.

## 5. An `ALLOW` is not enough: provenance at dispatch

`PermissionResult` gained a `via` field — a `DecisionVia`:

| Value | Produced by | Authorises a plugin call? |
| --- | --- | --- |
| `GRANT` | a stored grant for this principal | **yes** |
| `ONE_SHOT` | a single confirmation, consumed on use | **yes** |
| `POLICY` | the default policy (e.g. read-only auto-allow) | no |
| `DENY` | a policy refusal or the user | no |
| `CONFIRMATION` | a prompt that was accepted | no — the grant it implies must exist |

Dispatch accepts only the first two. Anything else is refused with
`DENIED_NO_GRANT`, `decision="policy_default"`, and a message naming the
provenance:

> capability `reference.metadata.validate` was allowed by policy (policy check),
> not by an explicit grant or a one-shot confirmation; plugin calls require a
> decision

An unset `via` (a third-party checker that predates the field) is treated as
unnamed and likewise refused. That is the conservative default: a new checker
that forgets to state its provenance cannot silently authorise anything.

Measured, not assumed: `test_a_read_only_capability_cannot_authorise_itself` counts
the capability's own invocations and asserts `runs == 0` after the call, so the
guard is proven by the absence of the side effect rather than by the return code
alone.

### 5.1 A grant is not an approval

Provenance answers "who said yes"; it does not answer "yes to what". A grant is a
row in the permission store, and anything that can write that store can create
one — including a generic `grant()` call that was never meant to touch plugins.
Before Phase 2.6.2, that was enough:

```
approve({A})            # B is declared, not approved
permissions.grant(principal_for(plugin), B)   # a plain grant, nothing else
→ the plugin executed B
```

Dispatch now asks a third question, through a `dispatch_guard` that
`PluginLifecycle` installs on the registry:

| Question | Asked by | Forged by writing the grants store? |
| --- | --- | --- |
| Did the manifest declare this? | the plugin's own claim | — |
| Is this principal allowed? | `PermissionManager.check` | **yes** |
| Did a person approve *this build* for *this capability*? | the dispatch guard | no — it reads the approval record |

The guard re-reads the store on every call, so a revocation in another process
takes effect on the next call rather than at the next restart. That costs one
store read per host call; correctness over micro-optimisation, and the cost is
stated here rather than hidden.

Adapters are untouched: `grant()` still means what it meant, and a host built
without a lifecycle has no dispatch guard at all. The stricter rule applies to
plugins, because a plugin is the one caller whose declaration is attacker-chosen.

Measured by `test_a_hand_written_grant_does_not_authorise_a_plugin_call`: it first
asserts that `PermissionManager.check` returns `allow` for the hand-written grant,
then asserts the call is refused with `decision="not_approved"` and
`runs == 0`. Both halves matter — without the first, the test could pass because
the grant was never effective in the first place.

## 6. Revocation: what it stops, and what it does not

`revoke(plugin_id)` does five things, in this order:

1. checks the transition to `revoked`;
2. **writes** the record (`state=revoked`) — this is the decision;
3. drains the admission queue for this plugin and blocks new admissions
   (`HostExecutor.revoke_plugin`);
4. deletes the plugin's grants and discards any pending one-shot allowance
   (`PermissionManager.revoke`);
5. reports `queued_stopped`, `running` / `still_running` and `stopped=False`.

Step 4 is not an implementation detail. The one-shot set lives in memory,
separate from the SQLite grants; deleting only the grants would leave a
confirmed-but-unused allowance that authorises exactly one more call after the
user revoked everything. `test_revocation_discards_a_pending_one_shot` proves it
by using one-shot, revoking, and calling again.

### 6.1 The linearisation point is admission

Work is in one of three places when a revocation lands, and the report keeps
them apart instead of summing them:

| Where the work is | What happens | Reported as |
| --- | --- | --- |
| waiting for a slot | removed from the queue, woken with `refusal=revoked` | `queued_stopped` |
| admitted, running | **not interrupted** — there is no preemption | `running` (and `still_running`) with `stopped=False` |
| arriving: past authorisation, not yet admitted | refused at admission by the shared revocation barrier | `refusal=revoked` |

The middle row is why `stopped` stays `False`. Some queued work *was* stopped, but
the field answers a narrower question — "was a running operation interrupted?" —
and widening its meaning would turn a true statement into a false one.

Step 2 and step 3 have no `await` between them, so the event loop cannot run
anything in the gap: once the record says `revoked`, the queue is already drained.

The barrier is **shared by every executor serving the same approval store**, keyed
by the canonical store path — so the guarantee is not limited to one host object.
Before Phase 2.6.3 each `HostExecutor` kept a private block set, which is only
correct while there is exactly one executor per store; two lifecycles over one
store left a call queued in the second host admissible after the first revoked.
`test_a_queued_call_in_another_host_never_runs_after_a_revocation` builds exactly
that: two lifecycles on one store, a gated call holding the only slot of the
second host, a second call parked behind it, a revocation committed by the first
host, and `runs == 1` after the gate opens.

Sharing is only worth anything if the identity of the store is decided the same
way every time, so §8.1 states the two keys and the failure mode. One consequence
belongs here: a guard that has already read the record can *lift* a block. It
therefore captures the barrier's generation for that plugin **before** the read
and lifts only through `lift_if_unchanged`. A revocation that lands between the
read and the lift moves the generation, the lift is refused, and the call is
refused as `plugin_not_approved` — the alternative would be a call admitted on an
approval that no longer exists.
`test_a_stale_guard_read_cannot_lift_a_revocation_that_landed_after_it` holds the
read open with a store subclass, revokes during the hold, and asserts the call
neither runs nor answers `ok`.

The counts in the report describe **the executor `revoke()` was called through**,
because that is the only queue it drains. A call waiting in the other host is not
counted in `queued_stopped` — it is refused when its turn comes instead. The test
asserts both halves, so "the other host's call was stopped" cannot be claimed from
a report that only ever inspected one queue.

Scope, stated rather than implied: the barrier is **process memory**. A second
*process* on the same store would keep its own, so a call it had queued could
still be admitted after another process revoked. Since Phase 2.6.4 that process
cannot exist: a second governed process over one store is refused at construction
by an exclusive OS lock (§8.2), which is the only way an in-memory barrier can be
an answer rather than a wish. The barrier itself is still not shared across
processes — the second process is simply not allowed.

There is a second, quieter window, and it is not the same one. `revoke()` has to
read the old decision, delete the grants, and write the new record; between those
steps the store still says `approved`. A guard in **another instance** could read
that stale record, lift the block the revoking instance had just placed, and admit
a call — measured, not hypothesised: the block went `True → False` and the call
ran. The fix is a shared *withdrawal mark* (`begin_withdrawal` /
`end_withdrawal` / `withdrawing`) on the barrier itself, so both lift paths —
`lift()` and `lift_if_unchanged()` — refuse while a withdrawal for that plugin is
in progress, and the refusal does not depend on which instance is asking.
`test_a_revoke_refuses_guards_in_another_lifecycle_before_it_writes` holds the
store write open and asserts the other instance's call is refused;
`test_another_lifecycle_cannot_lift_the_block_an_uninstall_placed` covers the
uninstall path; `test_both_lift_paths_refuse_while_a_withdrawal_is_in_progress`
pins the barrier's own rule directly, because the dispatch guard's early return
would otherwise hide a broken `lift_if_unchanged`.

The mark is not a lock held across the write: it is a flag that makes "I am
changing this decision" visible, and it is cleared in a `finally`. If the write
fails, the block this call placed is removed only if this call placed it — a
revocation that was already in force is not quietly undone. That is what
`test_a_failed_revocation_removes_only_its_own_block` asserts.

**The mark is owned (Phase 2.6.6).** A shared set plus an unconditional discard
meant that any withdrawal could clear any other withdrawal's mark: instance A's
uninstall parked between steps, instance B's `revoke` failed to write, B's
`finally` cleared *A's* mark, and a guard in a third instance — which captures the
generation *after* the failure, so the generation check agrees with it — read the
still-`approved` record and admitted the call. Reproduced before the fix, with the
uninstall parked inside its store write. `begin_withdrawal` now returns a
`Withdrawal` token and `end_withdrawal` needs it: the mark is a
`dict[plugin_id, set[token]]`, so more than one withdrawal can be in flight and
each can only release its own. The block is unchanged (a decision lifts it); only
the *mark* is owned.

Two consequences worth stating, because they are not obvious:

* `adopt_barrier` carries **blocks** but not **marks**. A copied mark has no
  owner and could never be released, which is a permanent refusal dressed as a
  safe one. Blocks are carried because a guard or `approve` releases them.
  Carrying a block bumps the generation, so a guard in another lifecycle that
  captured the generation just before refuses once — a temporary false refusal in
  the safe direction, never an over-permissive one.
* A token from another barrier, or for another plugin, releases nothing and
  returns `False`. Releasing twice is `False` too, not an error: the `finally`
  that runs on both the normal and the exceptional path must be idempotent.

`test_a_token_can_only_end_the_mark_it_was_issued_for`,
`test_a_token_from_another_barrier_releases_nothing`,
`test_an_unrelated_token_cannot_clear_a_mark` and
`test_a_failed_second_withdrawal_cannot_clear_the_first_ones_mark` pin this; the
`withdrawal-token-ownership` mutant restores the old unconditional discard and is
killed by the last of them.

Measured by `test_a_queued_call_never_runs_after_the_approval_is_withdrawn`,
which is deterministic rather than timing-dependent: a gated capability signals
that it started through a `threading.Event`, the second call is confirmed to be in
the queue through `HostExecutor.queued_for()`, and only then is the approval
withdrawn. The gate is opened afterwards and the assertion is `runs == 1` — the
queued call never executed.

```python
RevocationReport(
    plugin_id=...,
    revoked_grants=(...),  # ids that existed and were deleted
    was_active=True,
    queued_stopped=1,  # never started; nothing had to be stopped
    running=1,  # in flight at the moment of the decision
    still_running=1,  # the same number, under its older name
    stopped=False,  # no running operation was interrupted
)
```

The integration test asserts the in-flight call **still completes** after
revocation. That is the point: revoking an approval prevents the *next* call, and
saying anything stronger would be the same lie Phase 2.4 removed from the timeout
path.

### 6.2 When the revocation cannot be written

If the record write fails, nothing else happens: no queue is drained, no grant is
deleted, and the caller gets `plugin_lifecycle` with `revocation_persisted=False`
instead of a report. A revocation that reports failure must not have
half-happened, and a *report* is a claim that it did.

The failure mode in the other direction — the record is written and the grant
deletion fails — is classified the same way `uninstall()` classifies it, because
it is the same two facts (Phase 2.6.8b, C2):

| Detail | Value |
| --- | --- |
| code | `plugin_lifecycle` |
| `revocation_persisted` | `True` |
| `grants_withdrawn` | `False` |
| `__cause__` | the authority's own exception, chained |

Until 2.6.8b that failure escaped as whatever the authority happened to raise, so
the one distinction a caller most needs — "the decision was stored, the grants
were not withdrawn" versus "nothing happened" — was the one distinction with no
classification on it. Both halves are fail-closed: the record already says
`revoked`, so the dispatch guard refuses what the leftover grant would still
allow.

### 6.3 Re-running a revocation is a cleanup, not a second decision

`revoked → revoked` is legal (Phase 2.6.8b, C1), for the same reason the uninstall
self-loop is (§7.2): the decision is written before the grants are withdrawn, so a
failure at the last step leaves a persisted `revoked` record and a residual grant.
The only verbs that could otherwise finish the job are `install()` — which does
not touch grants — and `uninstall()` — which deletes the pin. A revocation that
failed at its last step would be unrecoverable by the verb that failed.

What the retry does: re-writes the same decision (same state, new `last_detail`),
drains whatever is still queued for the plugin, and withdraws the grants that are
still there. What it must **not** do, each asserted separately:

| Must not | Asserted by |
| --- | --- |
| create a grant | `grant_calls` is unchanged across the retry |
| change the decision | the record's `state` is still `revoked` afterwards |
| lift the block | `is_revoked()` is still true; only an approval lifts it (§2) |

`uninstall()` still works afterwards and still deletes the pin. That is why the
retry cannot be implemented as "write `approved`, then clean up": it would make the
plugin uninstallable by the verb that owns removal
(`test_a_retried_revocation_keeps_the_tombstone_semantics_of_a_later_uninstall`).

The scope of the claim: this is the same process re-running the verb. The barrier
is process memory, so a crash between the record write and the grant withdrawal
leaves a residual grant that a *new* process can only clear by touching the grants
store directly — the record refuses the plugin either way (§12).

## 7. Uninstall

`uninstall()` removes the pin (`deskweave.install.json`), the approval and the
grants. It refuses while host operations belonging to the plugin are still
running, unless `force=True`, because "uninstalled" next to a running operation is
a contradiction.

It **never deletes the `.wasm` file**. This tool does not delete the user's
files, and a half-deleted plugin directory is worse than a stale one.

### 7.1 The pin is deleted only if it is still *this* install

Until Phase 2.6.7b the last step was an unconditional `unlink`. Between the read
and the delete the path can be a different file: a replaced pin — a newer build
installed by something that does not take this lock, a hand-edited file, a
symlink. Deleting "whatever is at the path" destroys a newer install in the name
of an older decision.

The identity is `(plugin_id, wasm_sha256)`, both read **from the file** and both
written by `install()` from the manifest and from the bytes it hashed. A
directory, invalid JSON, or a file that cannot be read is `unreadable`; a symlink
is refused even when it resolves to a matching pin, because deleting the link is
not deleting what this install wrote and a link can point anywhere.

| What is at the path | Outcome | Reported as |
| --- | --- | --- |
| the pin this record describes | deleted | nothing (the normal case) |
| nothing | `absent` | nothing — nothing to do is a completed cleanup |
| a different `plugin_id` or digest | kept | warning: "it describes a different install" |
| a symlink | kept | warning: "it is a symbolic link" |
| a directory, or bytes that are not JSON | kept | warning: "its identity cannot be read" |
| the file cannot be unlinked | left visible | warning: "could not remove the install record" |

A pin left behind is **inert**: activation requires an `approved` record, and the
tombstone is not one. Keeping it is the fail-closed direction; deleting a
stranger's pin is the only outcome here that loses data.

What this does not buy: the read and the `unlink` are two syscalls, so a writer
that replaces the file *between* them still loses it. The check detects a
replacement that has already happened — it is not a compare-and-swap. Cooperating
writers are excluded by the store lock (§8.2); a non-cooperating one is not
(§12).

### 7.2 Re-running an uninstall is a cleanup, not a new decision

The first attempt can fail *after* the tombstone was written: the grants store
can be down, or the pin can have been kept because it was replaced. `uninstalled
→ uninstalled` is therefore a legal edge. Without it the only way to finish would
be `install` followed by `uninstall`, which needs the module to still be on
disk — so a failed uninstall would be unrecoverable by the verb that failed.

The retry cannot authorise anything: `approval` and `capabilities` are already
empty, and the pin check still refuses a stranger. It only retries the two
removals, and it never re-grants (`test_a_retry_after_a_failed_uninstall_does_not_regrant_anything`).

The lifecycle record is **kept** as a tombstone: `state=uninstalled`,
`approval=None`, `previous_approval=None`, `capabilities=()`. Deleting it would
make "this was uninstalled" indistinguishable from "this was never seen here" —
the difference an operator needs when a directory is handed to them. `inspect`
reports it with a `tombstone` note and `approval_in_force=False`.

Reinstalling after an uninstall does not restore the old approval: the tombstone
has no approval to inherit, and the guard reports plain `plugin_not_approved`.

## 8. Crash behaviour and write ordering

The two directions are not symmetric, so the order is chosen. The rule: a partial
outcome may leave the plugin able to do **less** than the record says, never more.

| Operation | Order | A failure leaves |
| --- | --- | --- |
| `install()` | pin first, lifecycle record second | a pin with no record → not activatable (safe direction) |
| `install()` (same bytes) | nothing at all, when the manifest still describes the same install | nothing; the previous decision is untouched |
| `approve()` | revoke stale grants → write the record → create missing grants | under-granted: the record claims a capability whose grant is missing, which dispatch refuses |
| `revoke()` | write the record → drain the queue → delete the grants | the record refuses what a leftover grant would allow |
| `uninstall()` | tombstone record → delete the grants → delete the pin, if it is still this install's (§7.1) | no approval → not activatable; a kept pin is inert |

The direction that matters is stated once: *grants that exceed the approval* is
the dangerous state, because dispatch reads grants. So grants are created **after**
the record that authorises them, and deleted **before** it is replaced. An earlier
version did the opposite for `approve()`, on the theory that "a record with no
grants" was the risk worth avoiding; that state is merely a plugin that cannot run,
while the mirror image is a plugin that runs something nobody approved.

The two stores cannot share a transaction — grants live in SQLite, decisions live
in the approval store — so there is no all-or-nothing option. What there is, is an
order in which every partial outcome is the safe one, plus an `inspect` that
reports `approved` and `granted` side by side and says so when they disagree.

`JsonApprovalStore` writes with `mkstemp` + `fsync` + `os.replace`, so a crash
leaves either the previous file or the new one. A corrupt or mis-shaped file
raises `plugin_lifecycle` rather than being parsed as "no approvals": silently
losing every approval would look identical to a fresh install, and would invite
re-approving without noticing.

`fsync` is called on the file, not on the containing directory. On Linux a rename
is not durable until the directory is synced, so a power loss immediately after
`os.replace` could leave the old file — the previous decision rather than a torn
one. Making that durable needs directory `fsync`, which Python does not expose
portably (`os.open` on a directory works on Linux, not on Windows). Recorded here
as a known limitation, not silently assumed: see §12.

### 8.1 One writer per instance, one barrier per store

Every transition takes an `asyncio.Lock` for the whole read-modify-write. That
lock is **per lifecycle instance, not per store**: two lifecycle objects are two
locks, and an `asyncio.Lock` cannot span event loops in any case. Phase 2.6.2's
documentation said `JsonApprovalStore` shared it across instances that open the
same path. The store has never held a lock and the claim was wrong; it is
corrected here rather than preserved.

What does hold for two instances in one process is weaker, and is stated as such:

* each transition is a read-modify-write and the record write is atomic
  (`mkstemp` + `os.replace`), so a lost update leaves one of the two decisions
  intact rather than a torn one;
* the invariant that survives **either** order is `approved == granted`,
  `test_two_lifecycles_never_leave_grants_that_disagree_with_the_record` asserts
  that invariant — not an ordering, and not a shared lock.

What *is* shared between instances in one process is the revocation barrier
(§6.1): the set of plugins whose admission is refused. Two hosts over one store
block the same plugins, because the record they both read is the same record.
Which barrier that is depends on what the store *is*:

| Store | Key | Why |
| --- | --- | --- |
| `JsonApprovalStore` (has a path) | `os.path.normcase(Path(path).resolve())` | two store objects on one file must reach one barrier; canonicalising also means a second spelling cannot mint a second one |
| `MemoryApprovalStore` (no path) | the store **object**, in a `WeakKeyDictionary` | there is no path to key on, and "one store, one barrier" is the guarantee that matters |
| a path that cannot be resolved | — | construction raises `plugin_lifecycle`; it does not fall back to a private barrier |

The last row is the point of the whole table. A private barrier is
indistinguishable from a shared one until a revocation fails to cross it, so
"cannot tell" has to be a refusal rather than a silent downgrade. The get-or-create
runs under a `threading.Lock`: without it, two threads constructing a lifecycle at
the same moment can each build a barrier, and a `WeakValueDictionary` is not safe
to iterate while another thread mutates it (documented dict behaviour: the
iteration raises `RuntimeError` rather than returning a stale answer).
`RevocationBarrier` is documented in `execution.py` and holds no `asyncio`
primitive — the admission check runs under the executor's own `threading.Lock` and
never awaits, so holding an `asyncio` lock across it would be a deadlock waiting to
be written.

Three tests pin this down: `test_two_lifecycles_over_one_memory_store_share_the_barrier`
(the pathless row), `test_two_lifecycles_on_one_store_share_the_revocation_barrier`
(the path row: two store objects on one file share, a third on another file does
not), and `test_a_store_path_that_cannot_be_resolved_refuses_to_construct` (the
refusal). The identity *rules* for a second spelling are exercised by the install
tests above, which compare the same canonical form.

The same identity question decides which *executor* may serve which barrier, and
Phase 2.6.4 closed the hole where it was answered by assignment: an executor
adopting a second, different barrier used to overwrite the first, so one host
silently answered for two stores and a revocation in one left the other
unblocked (measured: `A=False` and `B=True` after a single revoke). `adopt_barrier`
now refuses a second binding with `plugin_lifecycle` and carries the
withdrawal marks across from the previous barrier, so a plugin already being
revoked is not made admissible by the switch.
`test_an_executor_cannot_be_bound_to_two_stores` and
`test_two_distinct_memory_stores_do_not_share_a_barrier` cover the two halves;
`test_generations_advance_across_approve_revoke_uninstall_reinstall` pins the
generation ladder itself (0 → 1, an unchanged lift, then 2), so the staleness
check cannot be satisfied by accident.

A pathless store has one more failure mode, and it is now a refusal too: an
object that cannot be weakly referenced (a `__slots__` class without
`__weakref__`) cannot be a key in the registry, and the previous code caught the
`TypeError` and quietly minted a private barrier. Two lifecycles over the *same*
such object then disagreed — one blocked, the other admitted. Construction now
raises `plugin_lifecycle` instead, which is consistent with the path row:
"cannot tell" is a refusal, never a downgrade.
`test_a_pathless_store_that_cannot_be_weak_referenced_is_refused` asserts it.

Across **processes** the barrier does not exist at all: it is process memory, so
a call already queued in process B is *not* refused by a revocation committed in
process A. Since Phase 2.6.4 that configuration is refused instead of documented
away — §8.2 states what the lock does, and just as importantly what it does not.

### 8.2 One governing process per store

The barrier above is process memory, and a paragraph of documentation does not
stop a second process: it reads the same record, decides, writes back (losing
one of the two decisions), while its own barrier stays untouched by the other's
revocation. So two processes may not govern one store at the same time — and
`PluginLifecycle` now enforces that rather than assuming it. The exclusive OS
lock is taken **before the host is built**, so a refusal leaves nothing
half-constructed: no host, no executor, no dispatch guard.

| Decision | Why |
| --- | --- |
| The lock lives on `<store>.lock`, a separate file next to the store | every save replaces the store with `os.replace`, so a lock held on the store's old inode would stop meaning anything after the first write |
| The lock file is **never deleted** on release | deleting and re-creating it lets two processes lock two different inodes under one name — a lock that reports success and excludes nobody |
| The path is resolved before it is used as a key or as a file name | otherwise a relative path, or a second working directory, produces a second lock and a second registry key for one store |
| Windows uses `msvcrt.locking(LK_NBLCK)`, POSIX uses `fcntl.flock(LOCK_EX\|LOCK_NB)` | the native primitive for each; both non-blocking, because a governing process that blocks on startup is a hang rather than a refusal |
| A platform with neither is refused | there is no no-op fallback: a silent no-op lock is exactly the "documented but unenforced" state this replaced |
| In-process holders share one OS lock through a reference count | two lifecycles over one store are a supported configuration (§8.1); one lock per lifecycle would deadlock the second surface against the first |
| Release is `close()` / `aclose()`, with `weakref.finalize` as a backstop | `__del__` is not used, and a dropped object still lets go when it is collected |

Both failure modes are refusals, and both are structured:

| Code | Meaning |
| --- | --- |
| `plugin_store_locked` | another process holds it; `details` carries `path`, `lock_path`, `problem`. **Only** this case (Phase 2.6.6: a lock file that could not be *created* used to be reported here, which sends a caller into a retry loop over a condition that will never clear) |
| `plugin_lifecycle` | the lock could not be established at all — no platform primitive, unwritable directory, unresolvable path, a lock file that cannot be opened. Never "continue unlocked" |

`acquire()` creates the store's parent directory (`mkdir(parents=True,
exist_ok=True)`) because a lifecycle may be constructed before the first save, and
the save creates that directory too. It is the only write this module performs,
it is idempotent, and `test_acquire_creates_the_store_directory_and_says_so`
pins the contract rather than leaving it as an accident.

**Closing is a release *and* a revocation of authority (Phase 2.6.6).** `close()`
used to release the lock while leaving every entry point working: with another
process governing the store, a closed lifecycle still wrote it (measured — the
write succeeded and the record changed). `_closed` is now set permanently, and
every entry point that can write — `discover`, `install`, `approve`, `revoke`,
`uninstall`, `activate` — refuses with `plugin_lifecycle` (`details["lifecycle"]
== "closed"`), as do the two indirect paths: the activation guard and
`_dispatch_guard` (a refusal, because a closed lifecycle cannot see a revocation
committed by the process that took the store over). `inspect()` stays available on
purpose: it reads the store and hashes the module, and writes nothing.

**Refusing new work is not the same as being finished with the old work (Phase
2.6.7b).** `close()` used to release the OS lock in the same breath as it set
`_closed`. An operation already past its entry check keeps writing afterwards:
`revoke()` and `uninstall()` both park inside an `await` — the grants store, the
store write — and continue with more writes when it returns. Releasing first let a
second process take the store *while the first was still writing it*, which is the
one thing §8.2 exists to prevent. Measured with a real second process (2.6.7a): a
probe started after `close()` got the lock and read the store while an uninstall
was parked mid-write.

The lock is now held until the last in-flight operation returns:

| Piece | Why it is shaped this way |
| --- | --- |
| Every writing entry point is wrapped in `@_in_flight`: `discover`, `install`, `approve`, `activate`, `revoke`, `uninstall` | bookending the *entry points* is what makes the count mean "something can still write". `_save`'s `_require_open()` stays as the last line of defence for anything added later |
| `_register()` checks `_closed` and increments under one `threading.Lock` | a check followed by an increment would leave exactly the window a `close()` can slip into. It has to be one critical section, and it must be a `threading` lock: `close()` is synchronous and cannot await `asyncio.Lock` |
| The count is decremented in `finally` | a cancelled or raising operation must release the lock too. A count that leaks on cancellation would strand the store lock for the life of the process |
| `close()` releases only if the count is zero; the last operation out releases otherwise | `close()` cannot wait — it is synchronous and blocking the loop is not an option |
| `aclose()` is `close()` + a drain wait + `host.aclose()` | the waiting variant, and the only one that also closes the host it owns. It returns the host's leftover count, so "closed" is not reported while work is still running |
| The drain wait is a set of futures, one per `aclose()` call, each on **its caller's** loop | the operation that clears the count may run on a different loop and thread. `asyncio.Event.set()` from there appends to the waiter's loop without waking its selector, so the waiter parks forever — measured in 2.6.9a at 8 s, with the store already released and the host never closed. The wake is scheduled with `loop.call_soon_threadsafe`, the only thread-safe way to reach that loop (§8.2, Phase 2.6.9b R1) |

`close()` and `aclose()` therefore make different promises, and the difference is
the point: `close()` stops new work and *eventually* releases the store; only
`aclose()` waits, and only `aclose()` closes the `WasmPluginHost`. Calling
`aclose()` from inside one of the lifecycle's own operations is refused
(`details["lifecycle"] == "closing_self"`) rather than deadlocking: a task that
waits on itself hangs with no traceback, which is the one shutdown failure that
cannot be diagnosed after the fact.

A host that refuses to shut down is reported, not leaked. `aclose()` runs
`close()` first, so by the time `host.aclose()` is awaited the store lock is
already released and the instance already refuses work — which means a failure
here is *only* about the host, and reporting it as a bare `RuntimeError` would
leave the caller unable to tell "the host refused" from "the store is still
mine". 2.6.9b makes it `plugin_lifecycle` with `host_closed=False` and the
original exception chained, the same shape as `revocation_persisted` /
`grants_withdrawn` / `uninstall_persisted`: a partial outcome names the part
that did not happen. It deliberately records no "already attempted" flag, because
retrying is the supported remedy — `HostExecutor.aclose()` is re-entrant (it clears
`_pool` and its body contains no `await`), so a second `aclose()` finishes the job
and returns 0. `host_closed=False` means the shutdown did not report success; it
does not claim the host is running, because neither `WasmPluginHost` nor
`HostExecutor` exposes a closed state (§12).

`tests/integration/test_close_race.py` pins this with `asyncio.Event` gates — no
sleeps, no polling: close during a revocation and during an uninstall, both
witnessed by `probe()` in a second process; cancellation; an operation that
raises; `approve()` parked in its grant revocation; and `aclose()` returning only
after the gated operation has finished.

The cross-loop half cannot be pinned in-process, because it needs two loops on two
threads with the lifecycle closed from one of them. `tests/integration/
test_aclose_reliability.py` runs that scenario in a child process
(`tests/support/cross_loop_probe.py`) under a hard 90 s timeout, and has the child
verdict on itself: "returned with zero leftovers and closed the host once" has to
come from the process that parked, not from the loop that was woken. The same file
also holds the same-loop control, so a regression that makes `aclose()` return
immediately is caught rather than passed (§11).

**Construction installs the guard last.** `registry.dispatch_guard` was assigned
before the lock was taken and before the host existed, so a refused construction
left the caller's registry holding a guard bound to a lifecycle with no `_host` —
dispatch then raised `AttributeError` from inside the guard. The assignment now
happens after `WasmPluginHost` is built, and a construction that fails after
taking the lock releases it before re-raising.

What this does **not** buy, stated plainly: it is an *advisory* lock. A writer
that does not cooperate — an older build, a script that opens the JSON and dumps
it back, a person editing the file — is not stopped by it, and nothing here
claims otherwise. It also does not serialise two instances inside one process;
that is §8.1's weaker, invariant-based guarantee. What it does remove is the
configuration the design previously assumed away.

**The boundary of the guarantee.** Only `PluginLifecycle` takes the lock — it is
imported in exactly one place (`lifecycle.py`, in `__init__`). A caller that
builds `WasmPluginHost(activation_guard=<their own>, require_install_record=True)`
directly, without a lifecycle, gets no store lock and no shared barrier: that is
a governed *host*, not a governed *store*, and the single-writer rule does not
apply to it. Closing that would mean making the lock a property of the store
object rather than of the lifecycle, which is a larger change than this phase; it
is recorded here as the limit rather than left implied by "one governing process".

Nine tests in `tests/integration/test_store_lock.py` exercise this across real
process boundaries — the second party is `tests/support/lock_probe.py` started
with `sys.executable`, never a second object in one interpreter: exclusion;
governability after a normal release; governability after a holder is killed
with `os._exit` so no cleanup runs at all; a relative path from another
directory naming the same store; the same aliasing in the other direction; a
refused process getting no lifecycle and writing no bytes; the holder surviving
the attempt; `close()` being idempotent with the last holder releasing (checked
by a third process, because "the registry is empty" is not the same as "the OS
let go"); and the path-resolution contract itself. Six mutations were run
against them — including "the OS lock is never taken" and "a busy lock is
reported as success", the two ways this could silently become a no-op — and all
six were caught.

### 8.3 The store is read, not sampled

Every transition and every dispatch-guard check calls `store.load_all()` rather
than trusting a snapshot taken at startup. `mtime` is not enough to decide whether
a read can be skipped: it is written by whoever wrote the file, and a restored
backup sets it backwards. `test_the_store_is_read_rather_than_sampled` edits the
record and *backdates* the file before asserting that the second lifecycle refuses
to activate.

For `MemoryApprovalStore` the read is free. For `JsonApprovalStore` it is a small
file read per transition and per host call; that is the price of an answer about
the present.

### 8.4 When the approval store write fails

`approve()` reconciles grants with a decision across two stores, and the write is
the middle step. Before Phase 2.6.3 a failure there escaped as a bare `OSError`
with no structured result, so a caller could not tell "nothing happened" from
"the narrowing already ran". It now raises `plugin_lifecycle` with:

| Detail | Meaning |
| --- | --- |
| `approval_saved=False` | the decision was **not** stored; the record still holds the previous one |
| `revoked_stale=[...]` | the grants that were already deleted before the write failed |
| `grants_already_revoked=N` | the same list's length, for logs that summarise rather than enumerate |

The original exception is kept as `__cause__`. This is **not** a rollback and is
not described as one: the grants named in `revoked_stale` are gone while the
record still approves them, so the plugin can do *less* than the record says.
`inspect` reports `approved` and `granted` disagreeing, and dispatch refuses the
capability whose grant is missing.
`test_a_store_failure_while_narrowing_reports_the_partial_state` asserts both the
structured failure and that a call using the dropped capability neither runs nor
answers `ok`; `test_a_store_failure_while_expanding_keeps_every_grant_in_place`
covers the other direction, where nothing had been deleted yet.

A key fact this does **not** change: the store is policy state, not a trust root.
An approval that cannot be written is a refusal to record a decision, never a
decision with half of its consequences.

## 9. The trust boundary — five separate questions

These are constantly conflated. They are not the same control, and this project
implements them to very different depths.

| Question | Implemented? | Mechanism | What it does not give you |
| --- | --- | --- | --- |
| Are these the bytes that were installed? | **yes** (opt-in) | SHA-256 pin, `require_integrity` / `require_install_record` | author identity; a substituted module *and* pin is invisible |
| Did a person approve this capability set for this build? | **yes** | `PluginApproval`, the guard, `PermissionManager` grants | defence against someone who can rewrite the approval store |
| Who published this plugin? | **no** | — | nothing. Not claimed anywhere |
| Is the plugin's code isolated from the host? | **yes** | WASM sandbox, one `dw_host_call` import, fuel/time/memory caps | argument *meaning*: a future `read_file(path)` accepts any path |
| Can a running operation be stopped? | **no** | — | preemption. Timeouts end the wait, not the work; `stopped=False` |

An approval is a **local record of a local decision**, stored in the same
database (or the same directory) as the grants, under the same OS user. Someone
who can rewrite that store can approve anything — exactly as they could grant
themselves any adapter permission by writing the SQLite file. Making the approval
store tamper-evident needs the same missing pieces as publisher identity: a key,
a trust root, rotation and revocation.

What the approval *is* good for is the thing that was actually missing: a plugin
can no longer obtain authority without a decision that a human made, that is
bound to one build, that names the exact capability set, and that can be
withdrawn.

## 10. The CLI is read-only on purpose

`scripts/plugin_admin.py` has exactly two verbs:

```
python scripts/plugin_admin.py list examples/wasm-plugins
python scripts/plugin_admin.py inspect reference-metadata --plugins-dir examples/wasm-plugins
```

| Exit | Meaning |
| --- | --- |
| 0 | everything inspected is consistent |
| 2 | the directory or plugin was not found |
| 3 | drift: the module changed since install, or the pin is stale/invalid |

There is no `approve`, `revoke`, `activate` or `uninstall` verb, and a test
asserts that none exists (`test_the_cli_has_no_state_changing_verbs`). The reason is
that the authorisation boundary between a CLI and the running Runtime is not
designed yet: a CLI that could grant capabilities would be a second path to
authority, running as whichever OS user invoked it, with no record of the human
who decided. Until that is designed, the safe half is the half that only reads.

`inspect` deliberately re-reads the pin with `InstallRecord.from_dict` instead of
`verify_install`, because a verifier that raises on drift cannot *report* drift.
The CLI's job is to say "MODIFIED SINCE INSTALL", not to refuse to look.

## 11. Evidence

| Suite | Tests | Covers |
| --- | --- | --- |
| `tests/unit/test_plugin_lifecycle.py` | 27 | three sets stay separate; approval cannot invent or name an unoffered capability; subset approval; `check_approval` matrix; every transition edge; JSON store round-trip and corruption; discovery skips the install record; revocation targets only its own principal |
| `tests/integration/test_plugin_lifecycle_runtime.py` | 15 | real SQLite grants and real WAT modules: install creates no grants; unapproved capability refused at dispatch with `executed=False`; revocation denies the next call from the same loaded plugin and reports `still_running`/`stopped=False`; in-flight call still completes; new version/content/capability → `plugin_not_approved` naming the diff; failed install leaves no record; strict mode refuses without a pin; corrupt pin → `plugin_integrity`; uninstall removes pin+grants but not the module; uninstall refuses while running |
| `tests/integration/test_plugin_authorisation_boundary.py` | 6 | read-only capability cannot authorise itself (`runs == 0`); grant works; one-shot is consumed once; revocation discards a pending one-shot; an `ALLOW` with no provenance is not trusted; an undeclared capability is refused before policy runs |
| `tests/integration/test_plugin_approval_consistency.py` | 19 | same-bytes reinstall writes nothing (whole file compared) and the capability still runs; a changed build or capability set is not short-circuited; narrowing revokes the dropped grant and the plugin cannot execute it; narrowing after a revoke; a failed revoke never saves the new approval; `approval_in_force` distinguishes history from authority; a second lifecycle cannot activate after a revocation, even with a backdated store; two concurrent approvals leave `approved == granted`; uninstall leaves a tombstone; a queued call never runs after revocation (gate + queue counter, no sleeps); a revocation that cannot be recorded reports no success and applies nothing; a hand-written grant does not authorise a call; a cost refusal does not spend a one-shot; budget and pre-dispatch refusals record a state; a pinning host without a guard cannot be built; a crashing guard is recorded and refused |
| `tests/unit/test_plugin_admin_cli.py` | 9 | list/inspect output; honest "no install record" and "unknown (no approval store given)"; exit 3 on drift; exit 2 when missing; no state-changing verb exists |
| `tests/integration/test_lifecycle_integrity.py` | 28 | equivalent spellings of one manifest do not clear an approval; a record that disagrees with the manifest takes the full path; a guard defect is audited as a refusal, not an execution; a one-shot is returned by every refusal that did not start work and spent by every exit that did; two lifecycles share one barrier (path and pathless stores); a stale guard read cannot lift a revocation that landed after it; the manifest is the only authority for the ABI version; a failed approval is refused at every execution entry; a failed revocation leaves a working plugin; an unresolvable store path refuses construction at both layers |
| `tests/integration/test_governance_identity.py` | 11 | a relative store path is fixed at construction; a store reached through two names (relative + absolute, and a hard link) is refused or shares one barrier; another lifecycle cannot lift the block an uninstall placed; a revoke refuses guards in another lifecycle *before* it writes; a failed revocation removes only its own block; both lift paths refuse during a withdrawal; a pathless store that cannot be weak-referenced is refused; two distinct memory stores do not share a barrier; an executor cannot be bound to two stores; generations advance across approve/revoke/uninstall/reinstall; a queued call in a second lifecycle never starts after an uninstall |
| `tests/integration/test_activation_snapshot.py` | 8 | a direct `host.load` is refused without a fresh snapshot (and says which method to call); a stale cache cannot authorise a load after a revocation in another lifecycle; the guard judges the snapshot it is given, not the cache; a host without a guard still loads; a pin-equivalent swap mid-activation cannot change what was verified or recorded; a version swap mid-activation is refused before the engine; a manifest changed before activation is refused by the pin, and — without a pin — by the approval check, with the refusal recorded |
| `tests/integration/test_store_lock.py` | 9 | a second process cannot govern a held store (`plugin_store_locked` with `path`/`lock_path`); the store is governable after a normal release; after a holder dies with `os._exit` and no cleanup; a relative path from another directory names the same store and is refused; a relative *holder* blocks the absolute parent; a refused process gets no lifecycle and writes no bytes; the holder is not degraded by the attempt; `close()` is idempotent and the last holder releases (witnessed by a third process); `lock_path_for`/`acquire` resolve the path before using it as a key |
| `tests/integration/test_withdrawal_ownership.py` | 9 | A's uninstall parked in its store write, B's revoke fails, and the guard still refuses (the pre-2.6.6 regression); a token ends only the mark it was issued for; a token from another barrier or for another plugin releases nothing; a failed revocation releases only its own mark and leaves the record untouched; a successful uninstall releases its mark; two interleaved withdrawals keep the mark until the last one ends; ending a mark still refuses when the record says revoked; a mark placed by another instance refuses this one's guarded call |
| `tests/integration/test_lifecycle_closure.py` | 4 | six writing entry points refuse after `close()` with no byte written and no pin re-created; a closed lifecycle cannot authorise `host.load` or a dispatch; `close()` is idempotent, keeps other holders, and the last one releases (child process); a closed lifecycle cannot write while another process governs the store |
| `tests/integration/test_uninstall_failure.py` | 5 | a failed record write removes nothing (pin kept, grants kept, still activatable, `OSError` chained); a failed grant withdrawal is reported with `grants_withdrawn=False` and the record refuses the plugin; a pin that cannot be removed is logged and left visible; a retry does not re-grant; a *real* filesystem write failure (read-only store, Windows) takes the same path |
| `tests/integration/test_lifecycle_construction.py` | 4 | a refused lock leaves `dispatch_guard` untouched; a failed host initialisation releases the lock; a caller's pre-existing guard is not cleared; a successful construction installs a working guard |
| `tests/integration/test_lock_errors.py` | 6 | contention is still `plugin_store_locked` and leaves no in-process reference; an unopenable lock path is `plugin_lifecycle`, not a conflict; a parent directory that cannot be created is not a conflict; a failed acquire leaks no descriptor (Windows: the lock file stays deletable after the holder leaves); `acquire()` creates the store directory and nothing else; `release()` never deletes the lock file |
| `tests/integration/test_close_race.py` | 7 | `close()` during a revocation, and during an uninstall, keeps the lock (witnessed by `probe()` in a second process, and by the in-process key); `close()` during an uninstall does not delete a replacement pin; `aclose()` waits for the gated operation, then reports zero leftovers and has closed the host; a cancelled operation and a raising operation both release the count and the lock; `approve()` parked in its grant revocation cannot write the store after `close()` |
| `tests/integration/test_pin_identity.py` | 7 | a replaced pin, a pin for another build of the same plugin, an unreadable pin (directory / invalid JSON), a symbolic link that resolves to a matching pin, and an absent pin — all without closing or racing anything, so the pin rule is proven on its own; an honest uninstall still removes the pin and the grants; a retried uninstall completes the cleanup and still refuses the stranger |
| `tests/integration/test_aclose_reliability.py` | 5 | `aclose()` awaited from a **second event loop in another thread** returns, reports zero leftovers, closes the host once and has released the store lock (child process, hard timeout); the same-loop control still returns; a host that refuses to shut down is reported as `plugin_lifecycle` with `host_closed=False` and the cause chained, and a second call succeeds with the store already free; cancelling `aclose()` while draining, and while the host is closing, leaves the lifecycle able to finish the job later |
| `tests/integration/test_revoke_recovery.py` | 6 | a revocation that persisted `revoked` and then failed at the grants step is retried to completion — it withdraws the residual grant, creates none, does not change the record and does not lift the block; that failure is reported as `plugin_lifecycle` with `revocation_persisted=True` / `grants_withdrawn=False` and the authority's exception chained, while the mirror case (`revocation_persisted=False`) is the control; `uninstall()` still works and still deletes the pin after a retried revocation; an approval covering no capability does not lift the block, and one covering a capability does |

Mutation coverage in `scripts/wasm_guard_mutation_check.py`, each of which removes
one guard and requires the suite to go red. **61 mutations, 61 caught, 0 survived,
0 skipped** (`uv run python scripts/wasm_guard_mutation_check.py`) over 31 test
files — the 19 targets the harness started with, plus the eight suites 2.6.4 and
2.6.6 folded in (3 and 5), 2.6.7b's two, 2.6.8b's one and 2.6.9b's one. Phase
2.6.6 added ten of the mutations; 2.6.7b added five and re-pointed one whose anchor
had moved; 2.6.8b added three; 2.6.9b added two. Each addition was also attributed
to the single test that kills it, because "some test failed" is weaker than "this
assertion failed":

| Mutation | Guard removed |
| --- | --- |
| `activation-approval` | the guard is never called, so any build instantiates |
| `approval-escalation` | a manifest that grew a capability is accepted |
| `approval-content-binding` | an approval survives new bytes at the same version |
| `grant-provenance` | any `ALLOW` is treated as authorisation |
| `revoke-pending-one-shot` | pending one-shots survive revocation |
| `reinstall-clears-approval` | an upgrade inherits the old decision |
| `approval-grants-subset` | grants are created for everything requested |
| `install-idempotent` | every install rewrites the record, clearing a live approval |
| `approval-narrowing` | an approval only ever adds grants |
| `revocation-drains-queue` | calls queued before the decision still run |
| `dispatch-approval` | a grant is treated as an approval |
| `one-shot-restored` | a one-shot refused at admission is spent anyway |
| `canonical-manifest-path` | manifest paths are compared as strings, so two spellings look like two installs |
| `install-abi-version-identity` | a changed ABI version under the same pin counts as the same install, so the live approval survives |
| `guard-defect-is-a-refusal` | a defect in the approval check is audited as an execution |
| `guard-refusal-returns-one-shot` | a one-shot is handed back after the approval boundary refused without starting |
| `shared-revocation-barrier` | each lifecycle keeps a private block set over a shared path store |
| `shared-memory-barrier` | each lifecycle keeps a private block set over a shared pathless store |
| `no-stale-lift` | a block is lifted unconditionally, discarding a revocation that landed after the read |
| `expired-grants-are-not-authority` | expired grants count as current authority |
| `approval-store-failure-is-structured` | a store write failure escapes as a bare `OSError` |
| `withdrawal-token-ownership` | any token ends any mark for the plugin, so a failing withdrawal clears a live one |
| `withdrawal-mark-vetoes-both-lifts` | a mark no longer vetoes either lift path |
| `closed-lifecycle-still-writes` | closing releases the lock but leaves the instance able to write |
| `closed-lifecycle-still-vouches` | a closed lifecycle still authorises a dispatch or a load |
| `uninstall-store-failure-is-structured` | a failed uninstall record write escapes as a bare `OSError` |
| `uninstall-pin-before-record` | the pin is deleted before the record that decides the removal |
| `lock-create-failure-is-a-conflict` | a lock file that cannot be created is reported as contention |
| `lock-contention-is-a-broken-lock` | contention is reported as a broken lock, so callers stop retrying |
| `lock-failure-leaks-descriptor` | the descriptor leaks when the OS lock cannot be taken |
| `guard-installed-before-the-lock` | `dispatch_guard` is installed before the lock and the host exist |

Phase 2.6.7b added five, each killed by one named test:

| Mutation | Guard removed | Killed by |
| --- | --- | --- |
| `close-releases-lock-while-in-flight` | the lock is held until the last in-flight operation returns | `test_a_close_during_a_revocation_does_not_release_lock_early` — `probe()` gets the store while the revocation is parked |
| `aclose-does-not-wait-for-in-flight-work` | `aclose()` waits for the in-flight count to drain | `test_aclose_waits_for_in_flight_work_and_closes_the_host` — `closer.done()` after 50 scheduling turns |
| `uninstall-deletes-any-pin` | the pin is deleted only when the file still describes this install | `test_an_uninstall_in_flight_does_not_delete_a_replacement_pin` — the replacement is gone |
| `uninstall-follows-a-pin-symlink` | a symlink is refused even when it resolves to a matching pin | `test_a_pin_that_is_a_symbolic_link_is_kept` — `pin.is_symlink()` is false afterwards |
| `uninstall-cannot-be-retried` | `uninstalled → uninstalled` is a cleanup edge | `test_a_retried_uninstall_does_not_delete_the_replacement` — `cannot move … from uninstalled to uninstalled` |

`closed-lifecycle-still-writes` had to be re-pointed: `close()` no longer reads
`if self._store_lock is None`, so the old anchor matched zero times. A zero-match
anchor is reported as `SKIPPED` and fails the run — which is the behaviour that
makes the harness worth running, and the reason the anchor was updated rather than
dropped.

Phase 2.6.8b added three, each killed by the assertion that names the contract:

| Mutation | Guard removed | Killed by |
| --- | --- | --- |
| `revocation-cannot-be-retried` | `revoked → revoked` is a cleanup edge | `test_a_revocation_that_failed_at_the_grants_step_can_be_retried` — the retry is refused with `cannot move … from revoked to revoked`; `test_a_retried_revocation_keeps_the_tombstone_semantics_of_a_later_uninstall` kills it too, so the edge is proven load-bearing for removal as well |
| `revoke-grants-failure-escapes` | a failed grant withdrawal is classified as `plugin_lifecycle` | `test_a_grants_failure_is_reported_as_a_structured_lifecycle_error` — a bare `RuntimeError` where `PluginLifecycleError` was required |
| `empty-approval-lifts-the-block` | an approval covering no capability does not lift the revocation block | `test_an_empty_approval_does_not_clear_the_revocation_block` — `is_revoked()` is false after `approve(plugin, [])` |

Phase 2.6.9b added two, each killed by the assertion that names the contract:

| Mutation | Guard removed | Killed by |
| --- | --- | --- |
| `drain-wakeup-ignores-the-waiter-loop` | the drain waiter is completed on the loop that owns it, scheduled with `call_soon_threadsafe` | `test_aclose_awaited_on_a_second_loop_returns` — the child reports `stage='deadline'` with `aclose_returned=False` and `store_lock_released=True`, i.e. exactly the 2.6.9a defect: the store is free and nobody is left to say so. `assert 3 == 0`, 21.81 s |
| `aclose-host-failure-escapes` | a failed host shutdown is classified as `plugin_lifecycle` with `host_closed=False` | `test_a_host_shutdown_failure_is_structured_and_retryable` — a bare `RuntimeError: injected: the host would not shut down` where `PluginLifecycleError` was required |

`aclose-does-not-wait-for-in-flight-work` (added in 2.6.7b) had to be re-pointed:
`aclose()` no longer writes `await self._drained.wait()`, so the old anchor matched
zero times. It now removes `await self._wait_for_drain()` instead, and is killed by
`test_aclose_on_the_same_loop_still_returns` — `aclose() returned while an operation
was still in flight`, `assert not True`. The re-point matters beyond bookkeeping: it
is the only thing that proves the same-loop control assertion is load-bearing rather
than a test that would pass on any implementation.

The full anchor check before the run reports 61 mutations / 66 edits / 31 target
files and 0 problems (every anchor matches exactly once, no edit is a no-op, and
every mutant still parses). The files the run rewrites were hashed before and
after it: byte-identical, so the harness restored everything it touched. All three
mutations that touch `aclose()` were also attributed one at a time, and each one
restored `lifecycle.py` to `sha256=382a8da84f59bef6` before the next ran.

## 12. Known limitations

Stated rather than implied, because each one is a place where the design stops
short of a stronger guarantee:

| Limitation | Why it is not fixed here |
| --- | --- |
| Two processes that both ignore the lock can lose an update | the lock is **advisory**: a non-cooperating writer — an older build, a script, a hand edit — is not stopped by it, and the store is not tamper-evident either (§8.2, §9) |
| `aclose()` has no timeout | it waits for the in-flight count to drain and then calls `host.aclose()`, with no bound on either. A grants store that never returns parks shutdown indefinitely. Cancelling the caller does unwind the counter and release the lock (tested), so the exposure is "shutdown hangs", not "shutdown leaks" — and `close()` remains the non-waiting escape hatch. Not fixed in 2.6.8b: a timeout would need a decision about what to do with the operation it abandons |
| A `aclose()` that fails at the host step does not know whether the host is closed | 2.6.9b classifies the failure (`plugin_lifecycle`, `host_closed=False`, cause chained) and makes the retry work, so "the host refused" is distinguishable from "the store is still locked" — but `host_closed=False` means *the shutdown did not report success*, not *the host is running*. `WasmPluginHost` and `HostExecutor` expose no closed state, so the field cannot be more precise than the call's own outcome, and a caller that needs to know must retry and observe (§8.2, §11) |
| `install()` on a `revoked` record succeeds silently | re-pinning the same bytes writes nothing (§3.1), so `install()` returns a record that still says `revoked` without saying so. It grants nothing and lifts nothing — but a caller may read "install returned" as "installed". A refused-or-reported decision belongs to a phase that owns the install contract; 2.6.8b did not change it |
| A revocation cannot be finished across a restart | `revoked → revoked` makes the *same process* retry a failed cleanup (§6.3). After a crash the residual grant can only be removed by touching the grants store directly; the record and the block already refuse the plugin, so the leftover grant authorises nothing |
| A revocation does not cross a process boundary | it cannot: the barrier is process memory. A second *governed* process over one store is refused at construction (§8.2), so the case the barrier cannot cover does not arise; nothing claims to block a process that takes the file without the library |
| The pin identity check is not a compare-and-swap | the read and the `unlink` are two syscalls, so a non-cooperating writer that replaces the file between them still loses it. It detects a replacement that has already happened; cooperating writers are excluded by the store lock (§7.1) |
| `close()` cannot wait | it is synchronous, so it can only stop new work and defer the release to the last in-flight operation. A caller that needs the store to be free when the call returns must use `aclose()` (§8.2) |
| The `weakref.finalize` backstop for the store lock normally never runs | the registry holds a bound method (`registry.dispatch_guard`), so the lifecycle is usually not collectable at all. `close()` is the supported release path; the finalizer is a backstop for a lifecycle that was never wired into a registry, and the code says so rather than implying a safety net that is not there (§8.2) |
| A pathless store shares its barrier only when it is the *same object* | there is nothing else to key on; two distinct `MemoryApprovalStore` instances, or two wrappers over one underlying store, are indistinguishable from two unrelated stores (§8.1). An object that cannot be weakly referenced is now refused rather than given a private barrier |
| An executor serves exactly one store | binding it to a second barrier is refused, so a host cannot silently answer for two stores; a deployment that needs one executor for two stores builds two |
| The pin's `interface_version` can lag the manifest | `integrity.install` returns the old pin when the bytes are unchanged and does not compare the ABI version; the lifecycle record is written from the manifest, and the record is what the guard compares an approval against (§8.1) |
| A mid-activation swap is caught by the pin, not by a frozen file | `verify_install` re-reads the manifest to check the version, so a swap during activation is *refused* (fail-closed) rather than silently recorded; the manifest the guard judges and the record `activate()` writes come from the one snapshot it took (§4) |
| The canonical mutation harness covers these guards only as of Phase 2.6.6 | the three 2.6.4 suites are now in `TEST_TARGETS`, and 2.6.4's dedicated runs (6/6, 6/6, 4/5 — the survivor being an equivalent mutant, since disabling the identity check while `load()` still receives the object changes nothing observable) are superseded by the 51-mutation run. `locking.py` is mutated too |
| The store lock only constrains `PluginLifecycle` | a bare `WasmPluginHost(activation_guard=...)` takes no lock; making the lock a property of the store object is a larger change (§8.2) |
| `acquire()` creates the store's parent directory | a lifecycle may be built before the first save, and the save creates it anyway; it is the module's only write and it is tested (§8.2) |
| The approval store is not tamper-evident | it is a local record under the same OS user as the grants; needs a key and a trust root (§9) |
| `os.replace` is not followed by a directory `fsync` | not portable in Python; a power loss can leave the previous file (§8) |
| A guard crash is recorded but not categorised | `last_detail["error"]` names the exception type; there is no separate event for "the check itself failed" |
| Revocation does not interrupt running work | no preemption in this host (§6) |
| `inspect` re-hashes the module on every call | correctness over cost; a large module makes this the slow path |

See also: [plugin-installation.md](plugin-installation.md) for the bytes half,
[wasm-trust-boundary.md](wasm-trust-boundary.md) for the dispatch chain,
`docs/decisions/0017-plugin-approval-boundary.md` for why the provenance field
was added to the contract instead of special-casing the plugin host.
