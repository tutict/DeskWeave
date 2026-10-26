# Threat model — WASM plugins

Scope: code written by someone other than this project, compiled to
`wasm32-wasip1`, executed by `WasmPluginHost` inside the DeskWeave runtime
process.

**Adversary assumed:** the plugin author is not trusted and may be actively
hostile. They control the module bytes, the manifest, every field of every
payload, every length they report, and every byte of every response. They want
one of: the host's files, the host's network, the host's database, another
plugin's data, or simply to take the runtime down.

**Not assumed:** a compromised host, a compromised OS, malicious native code
inside the Wasmtime build, or an attacker with local code execution. Those are
outside what any user-space sandbox can promise.

## How to read the claims in this document

Not every sentence here has the same strength, so every claim is filed under one
of four headings:

| Heading | Means | How to use it |
| --- | --- | --- |
| **Verified** | A named test exercises the exact failure and asserts the outcome. For the controls, removing the control makes a test go red — checked by `scripts/wasm_guard_mutation_check.py` | Treat as evidence |
| **Depends on the host being correct** | The boundary is only as good as code outside the sandbox: a capability implementation, a permission policy, `PermissionManager` itself. The sandbox does not re-check any of it | Audit that code separately |
| **Not guaranteed** | Known and accepted. There is no control, and no plan to pretend there is one | Assume an attacker gets this |
| **Not verified on this platform** | Believed to hold, but only measured on Windows / CPython 3.12.9 / wasmtime-py 50.0.1. Behaviour on other platforms is inference, not measurement | Re-measure before relying on it |

Anything in this document that is *not* in the first table is a design intention.
Where the two disagree, the tests win — and Phase 2.3 was mostly the work of
finding the places where they disagreed.

## Design principles

1. **Deny by default, including when misconfigured.** No `PermissionChecker`
   wired means *deny*, mirroring `Adapter._authorize`. An unwired host must not
   be a permissive one.
2. **One door.** The import section may contain exactly one host function. Every
   effect on the world goes through it, so "what can a plugin do" has a
   one-function answer.
3. **A manifest is data, never authority.** `requested_capabilities` is a
   declaration of intent; the host decides. Declaration is necessary and never
   sufficient.
4. **Trust no claim about size, type or shape.** A length is read from a 4-byte
   header and clamped before any body is copied; both directions go through JSON
   Schema; a response is a JSON *object* or it is an error.
5. **A separate principal.** Plugins authenticate as `wasm-plugin:<id>`. A grant
   issued to `blender` or `browser` cannot be spent by a plugin that happens to
   use the same capability name.
6. **Failure is a value, not an exception.** `invoke()` returns
   `InvocationResult(ok=False, error=ErrorInfo)` on every path. A caller that
   checks one boolean is correct. In Phase 2.3 this stopped being a slogan and
   became a containment net at the end of `invoke()`.
7. **The host sets the budget.** Fuel, epoch, memory caps and host-call counts
   come from the host's ceilings, clamped in code. A manifest asking for more is
   a validation error, not a negotiation.

## 1. Verified security properties

Each row: the property, the test that would fail without it, and the mutation
check that proves the test is load-bearing. Test names are unique across the
suite; they live in `tests/integration/test_wasm_*.py`.

### 1.1 Authorisation

| Property | Test |
| --- | --- |
| An unregistered capability name is refused before any dispatch | `test_unregistered_capability_is_refused` |
| A registered capability with no grant is refused | `test_registered_but_ungranted_capability_is_refused` |
| Declaring a capability in the manifest does not authorise it | `test_declaring_a_capability_does_not_authorise_it` |
| A capability that is granted but *not declared* is still refused | `test_undeclared_capability_is_refused_even_though_it_is_granted` |
| A grant issued to another principal does not transfer | `test_grant_for_a_different_plugin_does_not_transfer` |
| A one-shot grant is spent by the first invocation and the second is refused | `test_a_one_shot_grant_is_spent_by_the_first_invocation` |
| Repeated failures never accumulate into a grant | `test_a_denied_plugin_cannot_retry_its_way_to_a_grant` |
| A plugin that fails still passes through the permission check on the next call | `test_a_failing_plugin_still_goes_through_the_permission_check` |

### 1.2 Request decoding

| Property | Test |
| --- | --- |
| 11 malformed request shapes are refused with a status, never an exception | `test_malformed_request_is_refused_and_audited` (parameterised) |
| Deeply nested JSON is refused (`RecursionError` is caught, not propagated) | `test_deeply_nested_request_is_refused_not_crashed` |
| A declared length beyond `max_input_bytes` is refused before any copy | `test_request_length_beyond_the_limit_is_refused` |
| A request pointer outside linear memory is refused | `test_request_pointer_outside_memory_is_refused` |
| Extra keys in the *envelope* are ignored and grant nothing | `test_unknown_envelope_keys_are_ignored_not_honoured` |
| Extra fields in the *payload* are rejected by the capability schema | `test_extra_payload_fields_are_rejected_by_the_capability_schema` |
| Invalid UTF-8 is refused as a decode error, not a host crash | covered by the `malformed_request` parameter set |

### 1.3 Writing back into guest memory

| Property | Test |
| --- | --- |
| A negative writable pointer is refused | `test_writable_buffer_at_a_negative_pointer_is_refused` |
| An output buffer smaller than the response is refused (`BUFFER_TOO_SMALL`) | `test_output_capacity_smaller_than_the_response_is_refused` |
| `dw_alloc` returning a pointer outside linear memory is a *plugin* error | `test_alloc_pointer_outside_memory_is_a_plugin_error` |

### 1.4 Load-time checks

| Property | Test |
| --- | --- |
| A manifest claiming another `interface_version` is refused | `test_manifest_interface_version_cannot_be_raised` |
| A module reporting another `dw_abi_version` is refused | `test_module_reporting_a_foreign_abi_is_refused` |
| An exported function with the wrong signature is a load error, not a later `TypeError` | `test_abi_signature_mismatch_is_a_load_error` (4 parameter cases) |
| A trap during instantiation is reported as a load error | `test_trap_during_instantiation_is_a_load_error` |
| A trap inside `dw_abi_version` is reported as a load error | `test_trap_in_abi_version_is_a_load_error` |

### 1.5 Resource budget

| Property | Test |
| --- | --- |
| Exactly `max_host_calls` host calls succeed; the next one fails the invocation | `test_budget_allows_exactly_the_declared_number`, `test_the_call_after_the_budget_fails_the_invocation` |
| The budget-hit audit record is written once, not once per blocked call | `test_budget_record_is_written_once_however_many_calls_follow` |
| Malformed calls consume the same budget as valid ones | `test_malformed_calls_consume_the_same_budget` |
| The budget is per invocation, not per plugin | `test_budget_is_per_invocation_not_per_plugin` |
| The budget ceiling cannot be raised by the manifest | `test_budget_ceiling_cannot_be_raised_by_the_manifest` |
| Every declared limit produces a measurable failure, not just a config value | `test_each_declared_limit_is_enforced` (5 parameter cases) |
| The input limit applies before the module runs | `test_input_limit_is_enforced_before_the_module_runs` |

### 1.6 Failure containment

| Property | Test |
| --- | --- |
| Eight failure modes all report `ok=False` with an `ErrorInfo` | `test_failure_is_never_reported_as_success` (parameterised) |
| A capability raising an exception becomes `plugin_host_error`, never a denial and never a success | `test_capability_exception_becomes_a_host_error_not_a_denial` |
| A host call that exceeds its timeout is contained and audited | `test_host_call_timeout_is_contained_and_audited` |
| An unexpected host-side exception is caught by the containment net | `test_an_unexpected_host_failure_is_contained` |
| `asyncio.CancelledError` is *not* swallowed by that net | `test_cancellation_of_the_invocation_is_not_swallowed` |
| Error text never contains an absolute path or a token | `test_errors_never_leak_paths_or_tokens` |
| A failed invocation leaves no state for the next one | `test_a_failed_invocation_leaves_no_state_for_the_next`, `test_a_failed_invocation_leaves_nothing_behind` |
| Linear memory does not survive between invocations | `test_memory_does_not_survive_between_invocations` |
| The plugin's own claim of success cannot outrank a recorded denial | `test_many_host_calls_are_capped`, plus the `denial-stickiness` mutation |

### 1.7 Execution governance (Phase 2.4)

| Property | Test |
| --- | --- |
| A blocking capability does not freeze the event loop (heartbeat keeps ticking) | `test_a_blocking_capability_does_not_freeze_the_event_loop` |
| An `async` capability is cancelled, and confirms it | `test_an_async_capability_confirms_its_cancellation` |
| A sync capability times out, keeps running, and is **not** called cancelled | `test_a_sync_capability_times_out_and_keeps_running` |
| A coroutine that swallows `CancelledError` produces `cancellation_requested`, not `cancelled` | `test_a_capability_that_ignores_cancellation_is_reported_honestly` |
| An unfinished operation keeps its slot and can refuse the next call | `test_an_unfinished_operation_still_holds_its_slot` |
| A host that declines to queue refuses immediately instead of backlogging | `test_a_host_without_a_queue_refuses_instead_of_backlogging` |
| The cost budget refuses *before* the capability runs | `test_the_cost_budget_stops_an_expensive_capability_before_it_runs` |
| Repeated requests cannot buy cheaper admission | `test_a_refused_call_is_never_executed_and_never_charged` |
| A capability failure is audited as admitted-but-failed, with a clean slot | `test_a_capability_failure_is_audited_as_admitted_but_failed` |
| Cancelling the invocation reaches an async capability | `test_cancelling_the_invocation_stops_an_async_capability` |
| Cancelling the invocation does **not** claim a sync capability stopped | `test_cancelling_the_invocation_does_not_claim_a_sync_capability_stopped` |
| A timed-out call is executed and charged exactly once | `test_a_timed_out_call_is_executed_and_charged_exactly_once` |
| Two plugins do not share an invocation budget | `test_two_plugins_do_not_share_one_invocation_budget` |
| Shutdown reports leftover work instead of waiting for it | `test_shutdown_reports_work_it_left_running_instead_of_waiting` |

### 1.8 Provenance and fairness (Phase 2.5)

| Property | Test |
| --- | --- |
| A module that changed after its manifest was written is refused | `test_a_module_replaced_after_the_manifest_was_written_is_refused` |
| A manifest that omits the hash is refused when integrity is required | `test_a_manifest_without_a_hash_is_refused_when_integrity_is_required` |
| A manifest that names a different module than the one loaded is refused | `test_the_declared_module_must_be_the_loaded_module` |
| A traversing or absolute `wasm_path` is refused before the filesystem is touched | `test_a_manifest_with_a_traversing_path_is_refused_at_parse_time` |
| A module reached through a symlink is refused | `test_a_symlinked_module_is_refused` |
| Re-installing different bytes over a pin is refused unless `replace=True` | `test_install_refuses_a_changed_module_unless_replaced` |
| A pin written by `install()` detects a later swap | `test_a_pinned_plugin_loads_and_a_later_swap_is_refused` |
| A manifest that *adds* a capability after being pinned is refused | `test_verify_detects_a_manifest_that_added_a_capability` |
| An integrity failure does not print the host's absolute path | `test_integrity_errors_do_not_leak_absolute_paths`, `test_a_load_failure_does_not_print_the_host_directory` |
| A second plugin is served before the first plugin's queued backlog | `test_a_second_plugin_is_served_before_the_first_plugins_backlog` |
| A plugin cannot exceed its own concurrency by queueing | `test_a_plugin_cannot_exceed_its_own_concurrency_by_queueing` |
| A timed-out operation keeps its slot, so a queued call cannot start | `test_a_timed_out_operation_keeps_its_slot_until_it_really_stops` |
| A full queue refuses deterministically rather than growing | `test_the_queue_is_bounded_across_plugins` |
| A queue timeout refuses without executing or charging | `test_a_queue_timeout_refuses_without_executing_or_charging` |
| A rate limit refuses before execution and charges nothing | `test_a_rate_limit_refuses_before_execution_and_charges_nothing` |
| One plugin's rate limit does not slow another | `test_one_plugins_rate_limit_does_not_slow_another` |
| Cancelling an invocation refuses its queued calls | `test_cancelling_an_invocation_refuses_its_queued_calls` |
| A capability failure leaves the queue and counters usable | `test_a_failure_does_not_corrupt_the_queue_or_the_counters` |
| Shutdown refuses queued calls and reports what is still running | `test_shutdown_refuses_queued_calls_instead_of_leaving_them_waiting` |
| Queueing does not starve the event loop | `test_queueing_does_not_delay_the_event_loop` |

Fairness is claimed as **bounded waiting**, not as strict fairness: round-robin
among waiters, no weighting, no latency guarantee.

### 1.9 Authorisation and revocation (Phase 2.6)

Before this phase, the answer to "may this plugin have this capability?" was
"whatever the manifest declared", because `PermissionManager` auto-allows
`READ_ONLY` risk and nothing distinguished *why* an `ALLOW` had been produced.

| Property | Test |
| --- | --- |
| A declared capability produces no grant; only `approve()` does | `test_installing_does_not_create_grants`, `test_the_manifest_creates_no_grants`, `test_declaring_a_capability_is_not_a_grant` |
| A capability allowed only by policy is refused at dispatch (`decision="policy_default"`) | `test_a_read_only_capability_cannot_authorise_itself`, `test_an_allow_without_provenance_is_not_trusted` |
| The refused capability body never runs (`runs == 0`) | `test_a_read_only_capability_cannot_authorise_itself` |
| An `ALLOW` with unset provenance is refused as unnamed | `test_an_allow_without_provenance_is_not_trusted` |
| An approval cannot name a capability the manifest never requested | `test_approval_cannot_invent_a_capability` |
| An approval cannot name a capability the host does not offer | `test_approval_cannot_name_a_capability_the_host_does_not_offer` |
| Approving a subset grants only the subset | `test_approving_a_subset_grants_only_that_subset` |
| A build with no approval is never instantiated | `test_activation_without_approval_is_refused`, `test_an_approved_capability_is_the_only_one_that_runs` |
| Changed content, version or ABI is refused under the old approval | `test_different_content_is_refused_even_at_the_same_version`, `test_different_version_is_refused`, `test_different_abi_is_refused`, `test_a_new_version_is_not_covered_by_the_old_approval` |
| A manifest that grew a capability reports `added` and requires re-approval | `test_a_new_capability_in_the_manifest_is_refused_as_escalation`, `test_a_new_capability_in_a_new_version_is_escalation` |
| Reinstalling does not restore an approval that was removed | `test_after_uninstall_a_reinstall_does_not_restore_the_old_approval` |
| Revocation denies the next call from the already-loaded plugin | `test_revocation_denies_the_next_call_from_the_same_loaded_plugin` |
| Revocation discards a pending one-shot allowance | `test_revocation_discards_a_pending_one_shot` |
| Revocation reports what it did not stop (`still_running`, `stopped=False`) and the in-flight call completes | `test_revocation_reports_running_work_and_does_not_claim_it_stopped` |
| Uninstall refuses while operations are running | `test_uninstall_refuses_while_an_operation_is_running` |
| A corrupt approval store is loud, never "no approvals" | `test_a_corrupt_store_is_loud_rather_than_empty`, `test_a_store_with_the_wrong_shape_is_refused` |
| The CLI cannot change state | `test_the_cli_has_no_state_changing_verbs` |

Two limits are part of the claim, not exceptions to it: an approval is a **local
record of a local decision** (someone who can rewrite the store can approve
anything), and revocation stops the **next** call, never the current one. See
[plugin-lifecycle.md](plugin-lifecycle.md) §6 and §9.

### 1.9b Consistency of the decision (Phase 2.6.2)

Phase 2.6 made "may this plugin have this capability?" answerable. Phase 2.6.2 is
about the answer staying true: a decision that was narrowed, withdrawn or never
made must stop being effective everywhere it is consulted, and a no-op must not
revoke anything.

| Property | Test |
| --- | --- |
| A narrowed approval deletes the grant it dropped, and the plugin cannot execute it (`runs == 0`) | `test_narrowing_an_approval_revokes_the_grant_it_dropped` |
| Narrowing after a revocation grants only the new set | `test_narrowing_after_a_revocation_grants_only_the_new_set` |
| A failed grant deletion is never followed by a saved approval | `test_a_failed_revoke_never_saves_the_new_approval` |
| Reinstalling the same bytes writes nothing at all (byte comparison of the store) and the capability still runs | `test_reinstalling_the_same_bytes_does_not_touch_the_record` |
| A changed build or capability set is **not** short-circuited | `test_a_different_build_is_not_short_circuited`, `test_a_new_capability_set_is_not_short_circuited` |
| A call queued before the withdrawal never runs (`runs == 1` of two calls) | `test_a_queued_call_never_runs_after_the_approval_is_withdrawn` |
| The revocation report separates queued from running work | `test_a_queued_call_never_runs_after_the_approval_is_withdrawn` |
| A revocation that cannot be recorded reports no success and applies nothing | `test_a_revocation_that_cannot_be_recorded_does_not_report_success` |
| A hand-written grant cannot authorise a plugin call the approval never covered | `test_a_hand_written_grant_does_not_authorise_a_plugin_call` |
| A second lifecycle does not act on a cached approval | `test_a_second_lifecycle_cannot_activate_after_a_revocation`, `test_the_store_is_read_rather_than_sampled` |
| Concurrent approvals cannot leave `approved ≠ granted` | `test_two_lifecycles_never_leave_grants_that_disagree_with_the_record` |
| `inspect` distinguishes a historical approval from one in force | `test_inspect_marks_a_revoked_approval_as_historical` |
| Uninstall leaves a tombstone instead of a gap | `test_uninstall_keeps_a_tombstone_instead_of_deleting_the_record` |
| A cost refusal does not spend a one-shot confirmation | `test_a_cost_refusal_does_not_spend_the_one_shot` |
| Every refusal records a terminal `state` | `test_a_budget_refusal_records_a_state`, `test_a_request_refused_before_dispatch_records_a_state` |
| A pinning host cannot be built without an approval guard | `test_a_pinning_host_without_an_approval_guard_is_refused` |
| A guard that raises is refused **and** recorded | `test_an_activation_guard_crash_is_recorded_and_refused` |
| Loading without a current approval snapshot is refused by the entry itself | `test_a_direct_load_is_refused_without_a_fresh_snapshot` |
| A cache older than the store cannot authorise a load | `test_a_stale_cache_cannot_authorise_a_load_after_a_revocation` |
| One activation uses one manifest snapshot | `test_a_swap_between_the_phases_cannot_change_what_was_verified`, `test_a_version_swap_between_the_phases_is_refused` |
| One store reaches one barrier, and one executor serves one store | `test_a_store_reached_through_two_names_is_refused`, `test_an_executor_cannot_be_bound_to_two_stores` |
| A decision being written cannot be lifted by another instance | `test_a_revoke_refuses_guards_in_another_lifecycle_before_it_writes`, `test_both_lift_paths_refuse_while_a_withdrawal_is_in_progress` |
| A second governed process cannot exist | `test_a_second_process_cannot_govern_a_held_store`, `test_a_holder_that_dies_without_releasing_does_not_pin_the_store` |

The residual limits are stated with the claims: cross-**process** writers that
ignore the lock are not serialised (the lock is advisory), and the approval store
is neither tamper-evident nor durable against power loss at directory level. See
[plugin-lifecycle.md](plugin-lifecycle.md) §8.1, §8.2 and §12.

### 1.10 The controls are load-bearing

`scripts/wasm_guard_mutation_check.py` deletes or corrupts one control at a time
and requires the suite to notice. It defines **sixty-one** cases; the table below
names fifty-six of them, the ones whose control lives in the load and execution
paths. The other five —
`revocation-cannot-be-retried`, `revoke-grants-failure-escapes`,
`empty-approval-lifts-the-block`, `drain-wakeup-ignores-the-waiter-loop` and
`aclose-host-failure-escapes` — sit in the approval, queue-drain and shutdown
paths described above; their definitions, anchors and expected failures are in the
script. Removing any of the sixty-one without a red test would mean the
corresponding "verified" claim is unearned.

| Mutation | Control it removes |
| --- | --- |
| `guest-write-bounds` | Both layers of the write-into-guest-memory guard (negative pointer, oversize, raising write) |
| `json-recursion` | The `RecursionError` catch around request decoding |
| `load-time-trap` | The `WASMTIME_FAILURES` catch around instantiation and verification |
| `record-refusals` | `bridge.reject(...)`, i.e. the audit record of a refused call |
| `host-call-budget` | The per-invocation host-call budget |
| `abi-signature` | The signature check at load time |
| `denial-stickiness` | The post-return re-check of recorded host calls |
| `host-failure-containment` | All three layers keeping a capability exception out of `invoke()` |
| `event-loop-offload` | Running capabilities in the executor instead of on the loop |
| `timeout-is-not-cancellation` | Reporting `timed_out` as `timed_out` |
| `slot-release-on-timeout` | Holding the slot until the operation actually finishes |
| `cost-check-before-execution` | Charging the cost budget before the capability runs |
| `cancellation-confirmation` | Waiting out the grace period before claiming a cancellation succeeded |
| `integrity-manifest-hash` | Comparing the loaded bytes to the manifest's `wasm_sha256` |
| `integrity-path-shape` | Rejecting `..` in a manifest-declared module path |
| `install-conflict` | Refusing to overwrite a pin without `replace=True` |
| `queue-fairness` | Rotating a served plugin to the back of the round-robin order |
| `queue-bound` | Refusing once the admission queue is full |
| `rate-limit` | Refusing a call that exceeds the per-plugin window |
| `queue-refusal-not-charged` | Charging only admitted calls, never a refused waiter |
| `activation-approval` | The guard that refuses to instantiate a build no approval covers |
| `approval-escalation` | Refusing a manifest that grew a capability after approval |
| `approval-content-binding` | Binding an approval to the module digest, not just the plugin id |
| `grant-provenance` | Requiring `GRANT`/`ONE_SHOT` provenance for a plugin call |
| `revoke-pending-one-shot` | Discarding in-memory one-shot allowances on revocation |
| `reinstall-clears-approval` | Clearing the approval when a plugin is re-pinned to a different build |
| `approval-grants-subset` | Creating grants for the approved set, not everything requested |
| `install-idempotent` | Doing nothing when the bytes being installed are already pinned |
| `approval-narrowing` | Revoking the grants a narrower approval no longer covers |
| `revocation-drains-queue` | Refusing calls that were queued before the withdrawal |
| `dispatch-approval` | Requiring an approval, not just a grant, to authorise a call |
| `one-shot-restored` | Handing back a one-shot that admission refused |
| `canonical-manifest-path` | Comparing manifest paths canonically instead of as strings |
| `install-abi-version-identity` | Treating a changed ABI version under the same pin as a different install |
| `guard-defect-is-a-refusal` | Auditing a defect in the approval check as a refusal |
| `guard-refusal-returns-one-shot` | Handing back a one-shot the approval boundary refused |
| `shared-revocation-barrier` | Sharing the revocation block between lifecycles over one store |
| `shared-memory-barrier` | Sharing the revocation block when two lifecycles share one in-memory store (no path to key on) |
| `no-stale-lift` | Refusing to lift a block when a revocation landed after the record was read |
| `expired-grants-are-not-authority` | Counting only unexpired grants as current authority (two edits: the local predicate and `list_effective_grants`) |
| `approval-store-failure-is-structured` | Reporting an approval-store write failure as `plugin_lifecycle` rather than a bare `OSError` |
| `withdrawal-token-ownership` | Letting any withdrawal token end any mark for the plugin, so a failing withdrawal clears a live one |
| `withdrawal-mark-vetoes-both-lifts` | A withdrawal mark no longer vetoing either lift path |
| `closed-lifecycle-still-writes` | Closing releasing the lock but leaving the instance able to write |
| `closed-lifecycle-still-vouches` | A closed lifecycle still authorising a dispatch or a load |
| `uninstall-store-failure-is-structured` | A failed uninstall record write escaping as a bare `OSError` |
| `uninstall-pin-before-record` | Deleting the pin before the record that decides the removal |
| `lock-create-failure-is-a-conflict` | Reporting a lock file that cannot be created as contention |
| `lock-contention-is-a-broken-lock` | Reporting contention as a broken lock, so callers stop retrying |
| `lock-failure-leaks-descriptor` | Leaking the descriptor when the OS lock cannot be taken |
| `guard-installed-before-the-lock` | Installing `dispatch_guard` before the lock and the host exist |
| `close-releases-lock-while-in-flight` | Holding the store lock until the last in-flight operation returns |
| `aclose-does-not-wait-for-in-flight-work` | `aclose()` waiting for in-flight work before it returns |
| `uninstall-deletes-any-pin` | Deleting the pin only when the file still describes this install (`plugin_id` + digest) |
| `uninstall-follows-a-pin-symlink` | Refusing to delete a pin that is a symbolic link, even when it resolves to a matching one |
| `uninstall-cannot-be-retried` | Allowing an uninstall that already recorded `uninstalled` to be re-run as a cleanup |

Result at the end of Phase 2.6.3: **39/39 caught, 0 survived, 0 skipped**, with the
working tree restored byte-for-byte (SHA-256 checked per file, not a text round
trip) and independently cross-checked against a pre-run hash inventory of every
tracked and untracked file.

Result at the end of the Phase 2.6.3 review: **41/41 caught, 0 survived, 0
skipped** after the two control fixes below were added, with the same byte-for-byte
restore check and the same independent inventory comparison — 196 tracked and 53
untracked files re-hashed against the pre-review baseline, only the five files this
review edited differ, nothing missing, nothing added.

Result at the end of Phase 2.6.4: **41/41 caught, 0 survived, 0 skipped**, on a
harness that was re-run *unchanged* after the hardening. Its anchors were
re-verified one by one against the rewritten `lifecycle.py`, `runtime.py` and
`execution.py` before the run — all 45 edits matched exactly once, so no
mutation silently degraded to `SKIPPED` — and the seven files it edits were
restored byte-for-byte against a pre-run hash inventory. The three guards this
phase added (the store lock, the withdrawal mark, the activation snapshot) were
mutation-checked with dedicated runs rather than folded into this table: 6/6 for
the lock, 6/6 for the barrier and identity rules, and 4/5 for the snapshot, the
survivor being an equivalent mutant (disabling the identity check while `load()`
still receives the parsed object changes nothing observable). Extending this
harness to cover them was recorded as a follow-up in plugin-lifecycle.md §12
rather than claimed as done; Phase 2.6.6 closed that follow-up by folding the
lock, the withdrawal mark and the closure rules (but not the activation
snapshot) into the table itself.

Result at the end of Phase 2.6.6: **51/51 caught, 0 survived, 0 skipped**, on a
harness grown to 27 test targets and 51 mutants. Every anchor was checked for a
single exact match before the run — one of them, `lock-create-failure-is-a-conflict`,
initially matched three times and was re-pointed at a two-line fragment instead of
being left to edit the wrong branch. The result is attributed per mutant rather
than reported as a total: the ten added mutants each died to a named test, which is
what makes the score mean something. The repository was hashed before and after the
run and the two inventories are identical.

Result at the end of Phase 2.6.7b: **56/56 caught, 0 survived, 0 skipped**, on a
harness grown to 29 test targets and 56 mutants. The five new mutants are the
guards this phase added (the lock's lifetime around an in-flight operation, and
the pin's identity on the way out); each is attributed to the single test that
kills it — `close-releases-lock-while-in-flight` to
`test_a_close_during_a_revocation_does_not_release_lock_early`,
`aclose-does-not-wait-for-in-flight-work` to
`test_aclose_waits_for_in_flight_work_and_closes_the_host`,
`uninstall-deletes-any-pin` to
`test_an_uninstall_in_flight_does_not_delete_a_replacement_pin`,
`uninstall-follows-a-pin-symlink` to `test_a_pin_that_is_a_symbolic_link_is_kept`,
and `uninstall-cannot-be-retried` to
`test_a_retried_uninstall_does_not_delete_the_replacement`. One anchor had to be
re-pointed: `closed-lifecycle-still-writes` targeted `close()`'s old early return,
which no longer exists, and a zero-match anchor is reported as `SKIPPED` and fails
the run. All 61 edits were checked for a single exact match and for still parsing
before the run; the repository was hashed before and after, and only the files
this phase edited differ.

Result at the end of Phase 2.6.8b: **59/59 caught, 0 survived, 0 skipped**, on a
harness grown to 30 test targets and 59 mutants. The three additions are the
recovery contracts the 2.6.8 audit found missing — a revocation that already
recorded `revoked` can be retried as a cleanup, its failure at the grants step is
classified instead of escaping raw, and an approval covering no capability does
not undo the block. Each dies to the assertion that names its contract rather
than to "the suite": `revocation-cannot-be-retried` to
`test_a_revocation_that_failed_at_the_grants_step_can_be_retried` (and to the
tombstone test as well, so the edge is proven load-bearing for removal too),
`revoke-grants-failure-escapes` to
`test_a_grants_failure_is_reported_as_a_structured_lifecycle_error`, and
`empty-approval-lifts-the-block` to
`test_an_empty_approval_does_not_clear_the_revocation_block`. All 64 edits were
checked for a single exact match, for a non-empty replacement and for still
parsing before the run; the three files the run rewrites were hashed before and
after it and are byte-identical, so nothing it touched was left mutated.

Result at the end of Phase 2.6.9b: **61/61 caught, 0 survived, 0 skipped**, on a
harness grown to 31 test targets and 61 mutants, 66 edits. The two additions name
the two defects the 2.6.9a audit measured. `drain-wakeup-ignores-the-waiter-loop`
removes the `call_soon_threadsafe` and completes the waiter from the thread that
happened to finish last; the child process then reports `stage='deadline'`,
`aclose_returned=False`, `store_lock_released=True` — the store free, the host
never closed, the caller never told — and the test fails on `assert 3 == 0` after
21.81 s. `aclose-host-failure-escapes` narrows the `except Exception` to
`except ValueError`, so an injected host failure surfaces as a bare
`RuntimeError: injected: the host would not shut down` where a `plugin_lifecycle`
outcome with `host_closed=False` was required. The one pre-existing mutant that
touches this code, `aclose-does-not-wait-for-in-flight-work`, was re-pointed from
`await self._drained.wait()` to `await self._wait_for_drain()` after the rewrite —
a zero-match anchor is `SKIPPED`, which fails the run, so re-pointing is what keeps
"the same-loop control still returns" proven instead of assumed. All 66 edits were
checked for a single exact match, for a non-empty replacement and for still
parsing before the run; the three mutations that touch `aclose()` were attributed
one at a time and each restored `lifecycle.py` to
`sha256=382a8da84f59bef6`. The full run's before/after hashes are byte-identical.

Result at the end of Phase 2.6.2: **32/32 caught, 0 survived, 0 skipped**, with the
working tree restored byte-for-byte (SHA-256 checked per file, not a text round
trip). Reaching that number found real problems, not nuisances:

- `guest-write-bounds` survived because the explicit `ptr < 0` check and the
  `except IndexError` were redundant — either one alone was enough. A redundant
  control is fine in production and useless as a test target. The mutation now
  removes both.
- The first `_normalise` helper compared `"\r\n" in raw` on a `bytes` object and
  raised `TypeError`, which the script reported as a surviving mutation. The
  lesson generalises: *a mutation harness that cannot tell "not caught" from
  "crashed" will report green.*
- `host-failure-containment` stopped matching in Phase 2.4 because the guard it
  targeted had *moved* — the capability no longer ran in `host.py`. The harness
  reported `SKIPPED`, which counted as not-proven and failed the run. That is the
  intended behaviour: a dangling anchor means the guard was never removed, so the
  result proves nothing.
- Phase 2.6.2 re-pointed two anchors for the same reason: `install()` and
  `approve()` were re-indented when the lifecycle lock was added, so
  `reinstall-clears-approval` and `approval-grants-subset` no longer matched. Both
  were updated against the new text rather than deleted, and both still catch the
  mutation. A moved anchor is a maintenance task, not an excuse to drop the
  mutation.
- Phase 2.6.3 re-pointed five more for the same reason: `event-loop-offload` (the
  executor call is now inside a `try`/`except BaseException`), `one-shot-restored`
  (the call is now `settle(outcome.executed)`), `install-idempotent` (the
  short-circuit became `if mismatch is None`), and `expired-grants-are-not-authority`
  twice over — once for the new `_grant_rows` fallback, then again after
  `ruff format` re-wrapped `list_effective_grants`. Before each run every anchor is
  checked for exactly one match, so a stale one fails loudly instead of counting as
  caught.

## 2. Properties that depend on the host being correct

The sandbox makes these *possible*. It does not make them *true*.

1. **Capability implementations are trusted code.** `HostCapability.run()` runs
   with full host authority. A capability that takes a path from the plugin and
   reads it is a vulnerability with no second layer behind it. The only
   structural guards are that registration is explicit and that the call passes
   through `PermissionManager` first — neither inspects the arguments.
2. **`PermissionManager`'s own semantics.** `READ_ONLY` implies auto-allow, and
   `code:execution` is always refused. A plugin capability that declares
   `READ_ONLY` is silently an unconditional grant. `ReferenceMetadataValidate`
   declares `LOW` for exactly this reason, and every future capability must
   repeat that decision deliberately.
3. **The host's ceilings.** Fuel, epoch period, memory cap and
   `max_host_calls` are chosen by whoever constructs the runtime. The defaults
   are defensible; a host that passes `max_host_calls=1024` and `fuel=10**12` has
   a plugin sandbox with the brakes removed.
4. **The manifest's honesty about what it needs.** `requested_capabilities` is
   compared against actual calls, but nothing verifies the list is *complete* or
   *minimal*. Over-requesting is the plugin's own risk surface; under-requesting
   only costs it a denial.
5. **A synchronous capability cannot be interrupted.** Since Phase 2.4 it no
   longer runs on the event loop — it runs in a bounded pool, so the loop stays
   responsive (measured by a heartbeat that ticks ~40 times during a 400 ms
   capability). But *stopping* it is still impossible: Python has no way to
   interrupt a running function, so a timeout means "we stopped waiting". The
   audit says `timed_out` and `confirmed_stopped: false`; the counter keeps the
   slot until the thread returns. An `async def` capability is genuinely
   cancellable, and the host waits out a grace period before claiming it was —
   `cancellation_requested` exists for the coroutine that swallows
   `CancelledError` and keeps working.
6. **Cancellation of the invocation only reaches work that can be asked.** The
   epoch bump stops the *guest*; `abort_scope` stops coroutine capabilities.
   Nothing stops a blocked thread, and `aclose()` returns the number it left
   running rather than pretending otherwise.
7. **One `Engine` per plugin, one `Store` per invocation.** Epoch scoping and
   memory isolation both rest on that construction being correct. A future
   optimisation that reuses a `Store` across invocations would quietly delete
   `test_memory_does_not_survive_between_invocations`.
8. **The audit log's integrity.** Refusals are recorded by the host and read back
   after the module returns. A host that drops the log sink has the plugin
   reporting its own success again.
9. **The installation step.** A plugin directory is trusted because the host was
   pointed at it. Integrity is an installation concern.
10. **Declared cost weights.** The budget is only as honest as the capability's
    `cost` attribute. Nothing validates that a number is plausible; a capability
    that declares `cost = 1` while launching a browser makes the budget
    decorative.
11. **Concurrency limits are per executor.** Two hosts with two executors have
    two "global" limits. Sharing one executor is possible; forgetting to is a
    silent capacity doubling.

## 3. Not guaranteed

Recorded so that nobody reads this document as a stronger claim than it makes.

1. **Cost is a declared weight, not a measurement.** `max_host_calls` bounds
   *how many* host calls a plugin makes (default 64, ceiling 1024) and
   `max_cost` prices them (default 64, ceiling 4096) using each capability's
   declared `cost`. That is the cheap half of the problem, and it is only as good
   as the declarations. What is *not* modelled: real CPU time, real memory, or a
   cost that varies with input. A capability that declares `cost = 1` and then
   renders a scene defeats the budget, and nothing in the host notices.
2. **No preemption.** A synchronous capability runs to completion. Killing it
   needs a process boundary and a real kill, which this design deliberately does
   not have.
3. **No admission across hosts.** Limits are per executor. Nothing limits how many
   `WasmPluginHost` instances exist, how many invocations happen per minute, or
   aggregate memory across plugins.
4. **Fairness is bounded waiting, not fairness.** Phase 2.5 added a bounded
   admission queue (16 waiters, 0.25 s) served round-robin across plugins, which is
   what removes the "hold every slot forever, free of charge" attack — the backlog
   no longer sits between another plugin and its turn. Rate limiting is a separate,
   optional third axis (`rate_limit_per_s`): it is **off** unless a capability or
   the host declares a value, so it is not part of the starvation fix. And none of
   this produces a guarantee an SLA could be written against: waiters are served
   round-robin with **no weighting**, so a plugin that
   calls once and a plugin that calls a thousand times get the same turn order;
   there is no latency bound beyond `max_queue_wait_s`; and a request that is
   granted and then runs slowly still occupies its slot for as long as it runs
   (item 2). Everything is per `WasmPluginHost` instance — two hosts share no
   queue and no rate window (item 3). Measurements and the exact claim are in
   `host-capability-execution.md` §"Fairness".
5. **Content pinning is not provenance.** `wasm_sha256` and
   `deskweave.install.json` detect that the bytes on disk changed after install.
   They do not identify who wrote them, and they are not a trust root: an
   attacker who can rewrite the `.wasm` can rewrite the manifest and the install
   record in the same motion, and the three will agree. There is no signature, no
   publisher key, no revocation list and no transparency log. See
   `plugin-installation.md` §8 for what the pin does and does not buy, and ADR
   0016 Decision 1 for why a PKI was not invented here.
6. **An approval is a local record, not a credential.** Phase 2.6 added a real
   authorisation gate — requested, approved and granted are separate sets, and
   only an explicit decision creates a grant. But the approval lives in the same
   store as the grants, under the same OS user. Someone who can write that store
   can approve anything, exactly as they could insert a permission row directly.
   It defends against *unattended* authority (a manifest granting itself), not
   against a local attacker. No signature covers the approval.
7. **Revocation does not stop work in progress.** `revoke()` deletes grants,
   discards pending one-shots and refuses everything still queued, so the **next**
   call fails; an operation already running completes and is reported as
   `running` / `still_running` with `stopped=False`. Item 2 is the reason, and no
   API can paper over it.
8. **A host with no approval guard has no approval check.** `WasmPluginHost()`
   constructed without an `activation_guard` has no notion of approval. Since
   2.6.2 that combination is *impossible* to reach while pinning bytes
   (`require_install_record=True` raises unless `allow_unguarded=True`), but a
   host that does not pin bytes is still unguarded by default. The gate is a
   property of `PluginLifecycle`, not of the package.
9. **The approval store has one *governed* writer, and the lock is advisory.**
   Two lifecycle instances in one process share one transition lock *per instance*
   plus one revocation barrier *per store*; two **processes** on one store are
   refused at construction by an exclusive lock on `<store>.lock` (2.6.4). What
   that does not cover: a writer that opens the JSON without the library — an
   older build, a script, a hand edit — is not stopped, the store is still not
   tamper-evident, and there is no CAS on the record. The guarantee is "two
   governed processes cannot coexist", not "the store has one writer". The
   Runtime is not wired to it yet, which is what keeps the rest theoretical today
   rather than exploitable.
10. **A plugin can lie about its own answer.** Output schema bounds shape, not
    truth. `reference.metadata.validate` mitigates this by returning problems
    only, never an echo of host data — but that is a property of that one
    capability, not of the framework.
11. **Timing side channels (Spectre-class)** are out of scope. Wasmtime does not
    claim to defend a plugin against a hostile host, and this design adds nothing.
12. **The wall clock is approximate.** Fuel is deterministic; the epoch deadline
    is not. A plugin can exceed its wall-clock budget by the epoch check interval
    — measured 154.9 ms for a 150 ms budget, i.e. ~3 %.
13. **stdout/stderr capture is bounded in memory, not in time.** `_BoundedSink`
    caps the buffer at 8 KiB; the cost of formatting the discarded bytes is real.
14. **A `proc_exit` is only as informative as the code.** `ExitTrap.exit_code` is
    reported but not interpreted; a plugin exiting `0` after malicious work looks
    tidy.
15. **No protection against a compromised host, OS or Wasmtime build.** A
    sandbox bounds the guest, not the thing running it.

## 4. Not verified on this platform

Everything below was measured on **Windows 11, CPython 3.12.9, wasmtime-py
50.0.1**. The contract is written to be platform-independent; the numbers are
not.

| Behaviour | What was measured here | Why it might differ |
| --- | --- | --- |
| Epoch interruption granularity | 154.9 ms actual for a 150 ms budget | The epoch thread's tick rate is OS-scheduler dependent; Linux/macOS may overshoot by less or more |
| Fuel determinism | `OUT_OF_FUEL` after a fixed instruction count, same every run | Fuel is specified per-instruction, but compiler/engine versions can change the count for the same module |
| `memory.read` past the end | Truncates silently, no exception | Documented as a Python-binding behaviour, not a Wasm spec guarantee; a different binding may raise |
| `memory.write` at a negative offset / past the end | Raises `IndexError` | Same caveat; `_guest_write` catches `IndexError`, `ValueError`, `TypeError` and the Wasmtime error types precisely because the exact class is not contractual |
| `wasmtime.Trap` vs `WasmtimeError` | Sibling classes — `issubclass(Trap, WasmtimeError)` is `False` | This is the root cause of most Phase 2.3 fixes. If a future version makes `Trap` a subclass, `WASMTIME_FAILURES` stays correct; if it introduces a third sibling, the containment net is the only backstop |
| `memory.grow` failure | Returns `-1`; no trap, no signal to the host | Guest-side convention, stable across platforms, but the host cannot observe it — a plugin that mishandles `-1` returns a wrong answer |
| `proc_exit` | Surfaces as `plugin_trapped` carrying `exit_code` | WASI preview 1 semantics; preview 2 / Component Model would differ |
| Path handling in manifests | Repo files are CRLF; the mutation harness normalises before matching | Path escaping and case-insensitivity differ on macOS and Linux |
| `threading.Timer` (epoch driver) | Sub-millisecond fire on Windows | Windows timer resolution is both worse and more variable than Linux; the measured overshoot may be pessimistic or optimistic elsewhere |
| `asyncio.CancelledError` propagation | Re-raised, not swallowed by the containment net | `BaseException` hierarchy is stable, but cancellation delivery points differ by event loop implementation |
| Thread-pool scheduling | 4 workers, refusal when full; measured on this machine | Thread creation cost and GIL contention differ by platform; the *counts* are portable, the latencies are not |
| `asyncio.wait(timeout=...)` precision | Heartbeat ticks ~40 times in 400 ms | Timer wheel resolution differs; assertions are thresholds, not equalities, for this reason |

The honest summary: the *structure* of every check is portable; the *timing* and
*exception-class* details are not. Anything in section 1 that depends on a
numeric threshold should be re-measured on the target platform before it is
quoted as a guarantee.

## The nine required failure classes

Every row is exercised by a named test — most in
`tests/integration/test_wasm_plugin_negative.py`, the rest where noted — and every
row is classified using the headings above.

| # | Threat | Control | Classification | Test |
| --- | --- | --- | --- | --- |
| 1 | Module missing or garbage bytes | `plugin_not_found` / `plugin_malformed` before any execution; a module missing `dw_invoke` or `dw_abi_version` is refused at load | Verified | `test_missing_module_is_not_found`, `test_malformed_bytes_are_refused` |
| 2 | Input violates the declared schema | `input_schema` validated **before** the module is instantiated | Verified, but the plugin chooses its own schema — the host's `max_input_bytes` is the only bound it does not control | `test_schema_invalid_input_is_rejected_before_execution` |
| 3 | Result is not valid / not what was promised | JSON parse, `isinstance(dict)`, `output_schema` validation, length-claim clamp | Shape verified; *truth* not guaranteed (3.4) | `test_non_json_response_is_rejected`, `test_response_lying_about_its_length_is_rejected`, `test_response_violating_its_own_output_schema_is_rejected` |
| 4 | Infinite loop (CPU burn) | Fuel metering; `OUT_OF_FUEL` at a deterministic instruction count | Verified here; determinism not verified elsewhere (4) | `test_infinite_loop_is_stopped_by_fuel` |
| 5 | Runs forever (wall clock) | Epoch interruption from a `threading.Timer` | Verified with ~3 % overshoot (3.7) | `test_endless_plugin_is_interrupted_by_the_wall_clock` |
| 6 | Memory exhaustion | `Store.set_limits` on **every** field: `memory_size`, `table_elements` derived from the same budget (one entry is one reference), and one memory / one table / one instance. A module whose minimum exceeds a cap is refused at instantiation; a write past the end traps | Verified; `memory.grow` returning `-1` is invisible to the host. Naming only `memory_size` bounded pages and left tables unlimited — a 20 000 000-entry table (160 MB) instantiated under a 1 MiB cap until this was fixed | `test_memory_cap_is_enforced_by_the_allocator`, `test_writing_past_the_memory_cap_traps_instead_of_corrupting`, `test_module_declaring_more_memory_than_allowed_never_instantiates`, `test_a_table_cannot_be_larger_than_the_memory_budget`, `test_a_module_cannot_declare_a_second_linear_memory` |
| 7 | Calls a capability it was not granted | Registry dispatch order: unknown → undeclared → schema → `PermissionManager`; distinct principal per plugin | Verified | `test_undeclared_capability_is_refused_even_when_granted`, `test_declaring_a_capability_is_not_a_grant`, `test_grant_for_an_adapter_id_does_not_authorise_the_plugin` |
| 8 | Built against an incompatible interface | `interface_version` in the manifest and `dw_abi_version` in the module both checked against `ABI_VERSION` at load; export signatures checked structurally | Verified | `test_manifest_claiming_another_abi_is_refused` (negative), `test_module_reporting_another_abi_is_refused` (contract), `test_abi_signature_mismatch_is_a_load_error` (adversarial) |
| 9 | Traps, exits or is cancelled mid-run | All traps translated (`plugin_trapped`, `plugin_timeout`, `plugin_resource_exhausted`); `ExitTrap` carries `exit_code`; cancellation bumps the epoch and re-raises `CancelledError` | Verified | `test_trap_is_reported_not_propagated`, `test_proc_exit_is_a_trap_not_a_host_exit`, `test_cancellation_interrupts_the_plugin_and_leaves_the_host_usable` |

## Adversarial threats beyond the required list

| Threat | Control | Classification |
| --- | --- | --- |
| **Swallow a denial and report success** | The runtime re-checks recorded host calls after the module returns; a denial outranks the plugin's own claim. Refusals are recorded even when the plugin ignores the status code (`bridge.reject`) | Verified — `record-refusals` and `denial-stickiness` mutations both go red |
| **Hide a refusal by never reading the status** | Same as above: the record is written by the host, not read from the return value | Verified |
| **Smuggle a path / token out through the response** | `ReferenceMetadataValidate` returns problems only, never an echo of the input; error text is built from fixed strings plus type names | Verified for this capability; depends on host for future ones (2.1) |
| **Forge a host-call response in linear memory** | The guest's buffer is written *by* the host; the plugin only chooses where and how much room. `_guest_write` bounds-checks the destination | Verified |
| **Return a pointer outside linear memory from `dw_alloc`** | Treated as a plugin error (`plugin_resource_exhausted`), not a host exception | Verified — this was a real escape before Phase 2.3 |
| **Pass a negative output pointer** | `_guest_write` refuses before touching memory | Verified — also a real escape before Phase 2.3 |
| **Exhaust the host's stack with nested JSON** | `RecursionError` is caught during decode | Verified — also a real escape before Phase 2.3 |
| **Make the host call a capability in a loop** | `max_host_calls` counts attempts (default 64, ceiling 1024) **and** `max_cost` prices them using each capability's declared weight; admission refuses before execution | Verified for count and weight; neither is a measurement (3.1) |
| **Freeze the host by calling a slow capability** | Capabilities run in a bounded pool, not on the event loop; the loop is free to serve everything else | Verified by heartbeat |
| **Leave threads behind by timing out repeatedly** | The slot is released on completion, not on timeout; a full plugin refuses the next call | Verified |
| **Overrun the host by calling from many plugins at once** | Global, per-plugin, per-invocation and blocking-pool limits, all refuse-when-full | Verified for one executor (3.3) |
| **Make the host wait for a capability that ignores cancellation** | Grace period then `cancellation_requested`; shutdown does not wait and returns the leftover count | Verified |
| **Make a capability throw to produce a false "allowed" audit entry** | Capability exceptions now produce `HOST_ERROR` and `decision="error"`, never `"allow"` | Verified |
| **Read another plugin's state** | One `Store` per invocation; linear memory cannot survive a call | Verified |
| **Interrupt another plugin** | One `Engine` per plugin, so `increment_epoch()` is scoped | Verified |
| **Flood stdout/stderr** | `_BoundedSink` caps capture at 8 KiB | Verified for memory; time is not bounded (3.8) |
| **Supply-chain: manifest and module disagree** | Both are read from disk; identity comes from the manifest and is only as honest as the installation step | Not guaranteed (3.5) |

## Gaps found by this phase

Phase 2.3 was written to prove the boundary, and the boundary lost five times.
Listed here because a threat model that only records successes is a marketing
document.

| # | Escape | Reachable without a grant? | Fix |
| --- | --- | --- | --- |
| 1 | `dw_alloc` returning a pointer past the end of linear memory made `invoke()` raise `IndexError` | Yes — it happens before any capability call | `_guest_write` bounds check; pointer validated at allocation |
| 2 | A negative output pointer made `invoke()` raise `IndexError` after authorisation had already passed | No, but reachable with one grant | `_guest_write` checks `ptr < 0` |
| 3 | ~60 000 levels of nested JSON made `invoke()` raise `RecursionError` | No, but reachable with one grant | `json.loads` wrapped; `RecursionError` produces `INVALID_ARGUMENT` |
| 4 | A host call exceeding its timeout made `invoke()` raise `TimeoutError`, losing the audit record — and the capability kept running | — | `FutureTimeoutError` handled in `_HostBridge`; outcome recorded; the non-cancellable part is documented as 2.5 |
| 5 | A trap in the `start` section or in `dw_abi_version` made `load()` raise a bare `Trap`; a wrong export signature raised `WasmtimeError` or `TypeError` | Yes — before any authorisation | `WASMTIME_FAILURES` tuple `(WasmtimeError, Trap)` everywhere; export signatures checked structurally at load |

Common root cause for 1, 2, 3 and 5: **`wasmtime.Trap` is not a subclass of
`wasmtime.WasmtimeError`** (verified: `issubclass(Trap, WasmtimeError) is
False`). Every `except wasmtime.WasmtimeError` in the original code was
therefore a partial catch. The systematic fix was to stop patching call sites
and introduce one error tuple, one guest-memory write helper, and one containment
net at the end of `invoke()` — in that order, because the net must be the last
resort and must not be the reason the earlier fixes look unnecessary.

## Gaps found by the Phase 2.6.2 review

The Phase 2.6.1 review was read-only and produced thirteen findings; the ten that
were fixed here are listed with what they actually allowed. All of them were
reproduced before being fixed, and each now has a test that fails without the fix
(the mutations in §1.10 are the proof that the tests bite).

| # | What it allowed | Reachable by | Fix |
| --- | --- | --- | --- |
| 1 | A narrowed approval kept the dropped grant: `approved=(A,)` with `granted=(A,B)` — and in 2.6 the grant was the only check on the call path | anyone who called `approve()` twice | `approve()` reconciles the granted set with the decision; `approved → approved` is a legal revision |
| 2 | A call queued before a revocation was admitted afterwards and ran | two concurrent invocations, one slot | `revoke_plugin()` drains the queue and blocks admission under one lock; admission is the linearisation point |
| 3 | A second lifecycle kept activating a build another process had revoked | two lifecycle instances on one store | every transition and every dispatch re-reads the store instead of trusting a startup snapshot |
| 4 | Reinstalling the same bytes cleared a live approval and left the plugin unactivatable | running `install()` twice | `install()` writes nothing when the build, capabilities and path are unchanged |
| 5 | `uninstall()` deleted the record and then wrote it back, leaving a tombstone nobody had decided on | calling `uninstall()` | the record is kept deliberately, with the approval and capability list cleared |
| 6 | A one-shot confirmation was spent by a call that admission refused | a cost/rate/capacity refusal on a confirmed call | `check()` reserves, `conclude(started=...)` hands it back when nothing ran |
| 7 | Pre-dispatch refusals and the budget marker audited `state=""` | any malformed request | `HostCallOutcome.state` is required and always a `CallState` |
| 8 | A strict host could be built with no approval check at all | `WasmPluginHost(require_install_record=True)` | that combination raises unless `allow_unguarded=True` |
| 9 | A plain `grant()` authorised a plugin call the approval never covered | writing the grants store | the dispatch guard asks the approval record per call |
| 10 | `inspect` showed `drift.ok=True` next to `state=revoked`, reading as "authorised" | after any revocation | `approval_in_force` is the field that means authorised; history is labelled |
| 11 | Two concurrent approvals could leave `approved ≠ granted` | two lifecycle instances in one process | each transition is serialised **per instance** and the record write is atomic; the invariant asserted is `approved == granted`, surviving either order (plugin-lifecycle.md §8.1) |
| 12 | A guard that raised something other than `PluginError` left the last event stale | a defect in the guard | the activation is refused and the failure is recorded with the exception type |
| 13 | `os.replace` is not followed by a directory `fsync` | power loss | **not fixed** — not portable; recorded as a limitation (§3, plugin-lifecycle.md §12) |

Row 11's original fix text said "one lock per store path, shared across
instances". That was never true: the transition lock is per `PluginLifecycle`, and
an `asyncio.Lock` cannot span event loops. Phase 2.6.3 corrected the claim and
added the mechanism that *is* shared — the revocation barrier, keyed by canonical
store path. What the test asserts is the invariant, not an ordering, and no test
in this repository demonstrates a shared transition lock.

## Gaps found by the Phase 2.6.3 review

The 2.6.2 audit was read-only and confirmed six findings. All six were fixed
without integrating the plugin package into the runtime, and each has a
deterministic regression test in `tests/integration/test_lifecycle_integrity.py`
plus a mutation in §1.10 that shows the test fails without the fix. Rows 20-22
were found afterwards by the 2.6.3 review itself, by probing the fixes rather
than trusting them; they are in the same file and the same harness.

| # | What it allowed | Reachable by | Fix |
| --- | --- | --- | --- |
| 14 | Installing the same bytes through a relative path cleared a live approval while reporting `unchanged` | `install()` called twice with two spellings of one manifest path | identity is compared on the canonical path plus plugin id, version, ABI version, digest and requested set; an uncanonicalisable path takes the conservative full write |
| 15 | A guard that raised a non-`PluginError` was audited `admitted=True executed=True state=failed` although the capability ran zero times | any defect in the approval check | `dispatch` catches it, audits `decision="not_approved" state="refused"`, logs the exception type and message, and returns neither to the caller |
| 16 | A one-shot stayed reserved when the approval guard, the queue or the cost bound refused the call | any refusal on a confirmed call | one idempotent `settle()` covers every exit: refused before start returns the reservation, started spends it |
| 17 | A call queued in a second host on the same store was admitted after a revocation committed by the first | two lifecycles over one approval store | the block set is a `RevocationBarrier` keyed by canonical store path, shared by every executor serving that store, and rechecked at admission |
| 18 | An expired grant was listed as current authority, so `approve()` believed a capability was already granted while dispatch refused it | a grant whose `expires_at` has passed | `list_effective_grants()` is the reconciliation input; `list_grants()` stays unfiltered as audit history |
| 19 | A failure writing the approval escaped `approve()` as a bare `OSError` with no structured result | a full disk or an unwritable store | the write is wrapped: `approval_saved=False`, `revoked_stale=[...]`, original exception as `__cause__`, partial state documented rather than called a rollback |
| 20 | A dispatch guard that had already read the record could *lift* a block placed by a revocation that landed in between, so the call it was authorising could still be admitted | a call whose approval check overlaps a `revoke()` | the barrier counts revocations per plugin; the guard captures the generation before reading the record and lifts only with `lift_if_unchanged`, otherwise it refuses `plugin_not_approved` |
| 21 | Two lifecycles over one *pathless* store each got a private barrier, so the row-17 guarantee silently did not hold for `MemoryApprovalStore` | two lifecycles constructed on one in-memory store object | barriers for pathless stores are keyed by the store object in a `WeakKeyDictionary`; path stores stay keyed by canonical path |
| 22 | Two threads constructing a lifecycle at once could each build a barrier for one store (and a `WeakValueDictionary` is not safe to iterate while another thread mutates it — documented dict behaviour, `RuntimeError`); an unresolvable store path fell back to a private barrier inside `except` | concurrent construction, or a path that cannot be resolved | get-or-create runs under a `threading.Lock`; a path that cannot be resolved refuses construction with `plugin_lifecycle` instead of degrading to an unshared barrier |

Row 17 is a *within-process* guarantee and stops there. The barrier is process
memory; two processes on one store have separate barriers, so a call already
queued in one process can be admitted after another process revokes. Phase 2.6.4
turned that from a documented limitation into a **refusal**: a second governed
process over one store cannot be constructed at all (row 30, plugin-lifecycle.md
§8.2). Nothing claims to stop a process that takes the file without the library —
the lock is advisory — and that is written down rather than implied.

## Gaps found by the Phase 2.6.4 review

A strict read-only review of four things — the revocation generation state
machine and every execution entry, store identity and barrier-registry lifetime,
the identity binding between manifest/pin/approval/loaded module, and whether
"single process per store" was enforced anywhere but in prose. Seven findings,
all reproduced with probes before a line was changed; the eighth item is the
constraint itself. Each fix has a deterministic test
(`test_governance_identity.py`, `test_activation_snapshot.py`,
`test_store_lock.py`) and was mutation-checked by breaking it deliberately.

| # | What it allowed | Reachable by | Fix |
| --- | --- | --- | --- |
| 23 | A relative store path plus a `chdir` produced **two barriers for one file**: one lifecycle recorded `revoked` while the other answered `is_revoked=False` and ran a queued call (`runs=2`) | constructing the store with a relative path, then changing directory | the path is resolved once, at construction, and that value is the only identity used by the barrier registry, the hard-link check and the lock; a path that cannot be resolved refuses construction |
| 24 | A hard link to the approval store was `os.samefile`-identical but got its own barrier, so a revocation never crossed | `ln approvals.json copy.json` | construction refuses any store whose `st_nlink > 1`, naming the link count |
| 25 | A pathless store that could not be weakly referenced fell into `except TypeError` and silently minted a **private** barrier — two lifecycles over the same object disagreed | two lifecycles over one `__slots__` store without `__weakref__` | the `TypeError` is a refusal (`plugin_lifecycle`), consistent with the path row: "cannot tell" is never a downgrade |
| 26 | A shared executor adopted the second store's barrier and overwrote the first: one `revoke()` left store A unblocked and store B blocked | one executor, two stores | an executor is bound to exactly one store's barrier; a second `adopt_barrier` raises, and withdrawal marks carry across on a legitimate switch |
| 27 | `uninstall()` blocked admission and *then* wrote the record; a guard in another lifecycle read the still-`approved` record during that window, admitted the call, and lifted the block (`True → False`) | two lifecycles over one store, a call overlapping an uninstall | a shared **withdrawal mark** on the barrier (`begin_withdrawal` / `end_withdrawal`); both lift paths refuse while it is set, regardless of which instance asks |
| 28 | `host.load()` judged the host's cached record, so a build revoked by another lifecycle was still instantiated (`LOADED digest=b0dcb207035d`) while `activate()` correctly refused it | calling `host.load()` directly instead of `activate()` | loading is not authorising: a guarded host refuses to load without the current approval snapshot, which only `activate()` obtains (under its lock, after refreshing the store) |
| 29 | `activate()` read the manifest twice, so a swap in between could mark build X `active` while build Y was instantiated | replacing the manifest during the activation | one manifest object for the whole activation; `load()` receives the parsed object, and `activate()` refuses if the plugin it gets back is not that object |
| 30 | "One process per store" existed only in documentation: two processes on one store each kept their own barrier, and a child process ran a call after the parent revoked (`runs=2`) | starting a second process on the same store | an exclusive advisory lock on `<store>.lock`, taken before the host is built; a busy store is `plugin_store_locked`, a lock that cannot be established is `plugin_lifecycle`, and there is no no-op fallback (plugin-lifecycle.md §8.2) |

Rows 23-26 are about *identity*: the same store has to reach one barrier, and one
store has to reach one executor. Rows 27-29 are about *time*: a decision that is
being changed, a cache that is older than the store, and two reads that can
disagree. Row 30 is about *scope*: the guarantee is per process, and the only
honest way to keep it is to refuse the second process.

## Gaps found by the Phase 2.6.5 review

The follow-up audit took the 2.6.4 fixes as claims to be tested rather than
results to be trusted, and found that three of them had exception paths nobody
had walked. All four were reproduced with probes before being changed
(`test_withdrawal_ownership.py`, `test_lifecycle_closure.py`,
`test_uninstall_failure.py`, `test_lifecycle_construction.py`,
`test_lock_errors.py`), and each fix has a mutant that restores the old behaviour
and is killed by a named test.

| # | What it allowed | Reachable by | Fix |
| --- | --- | --- | --- |
| 31 | The withdrawal mark had no owner: a second withdrawal whose store write failed cleared the *first* withdrawal's mark in its `finally`, and a guard then lifted the block and admitted a call while an uninstall was parked between steps | two lifecycles over one store; one uninstall overlapping one failing revoke | the mark is a set of **ownership tokens**; `end_withdrawal(token)` releases only the token it was issued for, a foreign or repeated token releases nothing, and `adopt_barrier` carries blocks but never marks |
| 32 | `close()` released the store lock but left every entry point working, so a closed lifecycle kept writing after another process had taken the store | `close()`, then any transition | a permanent `_closed` state: six writing entry points, the activation guard and the dispatch guard refuse with `plugin_lifecycle` (`details["lifecycle"] == "closed"`); `inspect()` stays read-only by design |
| 33 | `uninstall()` removed grants and the pin *before* writing the record, so a failed write left the pin deleted under a record that still said `approved`, and the failure escaped as a bare `OSError` | a store write failure during an uninstall | record → grants → pin; a failed record write leaves pin, grants and approval untouched and raises a structured `plugin_lifecycle` with the `OSError` chained; a pin that cannot be removed is logged, not swallowed |
| 34 | `registry.dispatch_guard` was installed before the lock was taken and before the host existed, so a refused construction left a guard whose lifecycle had no `_host` — dispatch raised `AttributeError` from inside the guard | constructing a lifecycle over a store another process governs | the guard is installed last, after the host; a construction that fails after taking the lock releases it before re-raising |
| 35 | A lock file that could not be *created* (read-only directory, a directory in its place) was reported as `plugin_store_locked`, i.e. as contention, sending callers into a retry loop over a condition that never clears | an unwritable store directory | "busy" and "broken" are separate: only real contention is `plugin_store_locked`; create/open failures are `plugin_lifecycle` with the `OSError` chained |

Row 31 is the sharp one: the mark is a *safety* mechanism, and its failure
direction was "admit", not "refuse". Rows 32-34 are the same shape in different
places — a release that released more than intended (the lock without the
authority, the pin before the decision, the guard before the guard's owner).

## What would break this design

- Granting a capability that hands the plugin a path, a URL or a handle, then
  letting the plugin choose the target. The sandbox protects the *boundary*, not
  the *arguments* — a `read_file(path)` capability with the path from the plugin
  is a different security model wearing the same shape.
- Letting a plugin ship native code, or an import that reaches one (`dlopen`,
  FFI). That dismantles the guarantee rather than weakening it.
- Adding a second import for convenience ("just a small `clock_time_get`"). Each
  one is a channel, and the single-door property is what makes this auditable.
- Auto-allowing a capability because it is "read-only". `PermissionManager`
  treats `READ_ONLY` as auto-allow — which is exactly why
  `ReferenceMetadataValidate` declares `LOW`, not `READ_ONLY`. Any future plugin
  capability that quietly copies that pattern is an auto-granted plugin call.
- Reusing a `Store` across invocations for speed. It would delete the isolation
  property that `test_memory_does_not_survive_between_invocations` exists to
  protect, and nothing else would notice.
- Calling `HostCapability.run()` from a thread or process without revisiting 2.5.
  The current non-cancellable behaviour is a *consequence of the synchronous
  interface*, not a decision — changing the interface is the right fix, silently
  relying on the timeout is not.
