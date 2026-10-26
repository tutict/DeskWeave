"""Verify that the WASM boundary tests actually bite.

A security test that passes with the guard removed proves nothing. This script
removes each guard in turn, runs the sandbox suite, and requires it to fail;
then it restores every file **byte for byte** and proves that with a SHA-256
comparison rather than by reading the text back (text mode would normalise line
endings and quietly "restore" a different file).

A mutation may touch more than one place, because some guarantees are
deliberately layered: removing only the inner guard leaves the outer one armed,
and a test that still passes is telling the truth about that. Those entries list
every edit that has to go for the property to be lost.

    uv run python scripts/wasm_guard_mutation_check.py

Exit code 0 only when every mutation was caught. A mutation whose anchor cannot
be found is reported as SKIPPED and counted as *not proven*, because an anchor
that no longer matches means the guard was never actually removed.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "packages" / "plugin-wasm" / "src" / "deskweave_plugin_wasm"
RUNTIME = PACKAGE / "runtime.py"
HOST = PACKAGE / "host.py"
LIFECYCLE = PACKAGE / "lifecycle.py"
LOCKING = PACKAGE / "locking.py"
MANAGER = REPO_ROOT / "runtime" / "src" / "deskweave_runtime" / "permissions" / "manager.py"

#: The suite each mutation must break.
TEST_TARGETS = [
    "tests/unit/test_wasm_abi.py",
    "tests/unit/test_wasm_manifest.py",
    "tests/contract/test_wasm_plugin_contract.py",
    "tests/integration/test_wasm_plugin_runtime.py",
    "tests/integration/test_wasm_host_capability.py",
    "tests/integration/test_wasm_plugin_negative.py",
    "tests/integration/test_wasm_authorisation_adversarial.py",
    "tests/integration/test_wasm_host_budget.py",
    "tests/integration/test_wasm_invariants.py",
    "tests/integration/test_wasm_host_execution.py",
    "tests/unit/test_wasm_integrity.py",
    "tests/integration/test_wasm_install_integrity.py",
    "tests/integration/test_wasm_fair_scheduling.py",
    "tests/unit/test_plugin_lifecycle.py",
    "tests/unit/test_plugin_admin_cli.py",
    "tests/integration/test_plugin_lifecycle_runtime.py",
    "tests/integration/test_plugin_authorisation_boundary.py",
    "tests/integration/test_plugin_approval_consistency.py",
    "tests/integration/test_lifecycle_integrity.py",
    # Phase 2.6.4's guards, folded in by 2.6.6: they were covered by dedicated
    # mutation runs before, which is not the same as being covered by this one.
    "tests/integration/test_governance_identity.py",
    "tests/integration/test_activation_snapshot.py",
    "tests/integration/test_store_lock.py",
    # Phase 2.6.6's own guards.
    "tests/integration/test_withdrawal_ownership.py",
    "tests/integration/test_lifecycle_closure.py",
    "tests/integration/test_uninstall_failure.py",
    "tests/integration/test_lifecycle_construction.py",
    "tests/integration/test_lock_errors.py",
    # Phase 2.6.7b's guards: the lock's lifetime around an in-flight operation,
    # and the pin's identity on the way out.
    "tests/integration/test_close_race.py",
    "tests/integration/test_pin_identity.py",
    # Phase 2.6.8b's guards: the three recovery contracts the 2.6.8 audit found
    # missing — retrying a revocation, classifying its failure, and not letting an
    # empty approval undo a block.
    "tests/integration/test_revoke_recovery.py",
    # Phase 2.6.9b's guards: closing a lifecycle whose operation runs on another
    # event loop, and classifying a host that refuses to shut down.
    "tests/integration/test_aclose_reliability.py",
]


@dataclass(frozen=True)
class Mutation:
    name: str
    guard: str
    edits: tuple[tuple[Path, str, str], ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="guest-write-bounds",
        guard="every guard on a write into guest memory (negative, oversize, raising)",
        edits=(
            (
                RUNTIME,
                """    if ptr < 0 or len(data) == 0:
        return ptr >= 0
    try:
        available = memory.data_len(store)
    except WASMTIME_FAILURES:  # pragma: no cover - detached memory is a host bug
        return False
    if ptr + len(data) > available:
        return False
    try:
        memory.write(store, data, ptr)
    except (IndexError, ValueError, TypeError, *WASMTIME_FAILURES):
        return False
    return True""",
                """    memory.write(store, data, ptr)
    return True""",
            ),
        ),
    ),
    Mutation(
        name="json-recursion",
        guard="RecursionError when decoding a deeply nested request",
        edits=(
            (
                RUNTIME,
                "            except (ValueError, TypeError, RecursionError) as exc:",
                "            except (ValueError, TypeError) as exc:",
            ),
        ),
    ),
    Mutation(
        name="load-time-trap",
        guard="traps raised while instantiating and verifying the module",
        edits=(
            (
                RUNTIME,
                """        except WASMTIME_FAILURES as exc:
            raise plugin_error(
                PluginMalformedError,
                f"module cannot be instantiated: {exc}",""",
                """        except wasmtime.WasmtimeError as exc:
            raise plugin_error(
                PluginMalformedError,
                f"module cannot be instantiated: {exc}",""",
            ),
        ),
    ),
    Mutation(
        name="record-refusals",
        guard="recording a refused host call so the plugin cannot hide it",
        edits=(
            (
                RUNTIME,
                """                bridge.reject(capability, status, reason)
                return status""",
                """                return status""",
            ),
        ),
    ),
    Mutation(
        name="host-call-budget",
        guard="the per-invocation host-call budget",
        edits=(
            (
                RUNTIME,
                """        if self._calls <= self._max_calls:
            return True""",
                """        if True:
            return True""",
            ),
        ),
    ),
    Mutation(
        name="abi-signature",
        guard="the ABI signature check at load time",
        edits=((RUNTIME, "            if found != (params, results):", "            if False:"),),
    ),
    Mutation(
        name="denial-stickiness",
        guard="re-checking host calls after the plugin claims success",
        edits=(
            (
                RUNTIME,
                """        denial = self._first_denial(bridge, manifest.plugin_id)
        if denial is not None:
            return self._failed(manifest.plugin_id, denial, started, bridge=bridge, sink=sink)""",
                """        denial = None
        if denial is not None:
            return self._failed(manifest.plugin_id, denial, started, bridge=bridge, sink=sink)""",
            ),
        ),
    ),
    Mutation(
        name="host-failure-containment",
        guard="all three layers that keep a capability exception out of invoke()",
        edits=(
            (
                RUNTIME,
                "            except Exception as exc:  # containment net: see the comment below",
                "            except MemoryError as exc:  # containment disabled",
            ),
            (
                RUNTIME,
                "        except Exception as exc:  # a capability bug must not escape the sandbox",
                "        except MemoryError as exc:  # containment disabled",
            ),
            (
                PACKAGE / "execution.py",
                """        except Exception as exc:
            return result(
                CallState.FAILED,""",
                """        except MemoryError as exc:
            return result(
                CallState.FAILED,""",
            ),
        ),
    ),
    # -- Phase 2.4: execution governance -------------------------------------
    Mutation(
        name="event-loop-offload",
        guard="that a capability no longer runs on the event loop thread",
        edits=(
            (
                PACKAGE / "host.py",
                """        try:
            outcome = await self.host_executor().run(
                plugin_id=plugin_id,
                capability=capability,
                handler=handler,
                payload=payload,
                scope=budget,
                timeout_s=DEFAULT_HOST_CALL_TIMEOUT_S if timeout_s is None else timeout_s,
            )""",
                """        try:
            _inline = handler.run(dict(payload))
            outcome = ExecutionResult(
                state=CallState.COMPLETED, data=_inline, reason="mutation: inline"
            )""",
            ),
        ),
    ),
    Mutation(
        name="timeout-is-not-cancellation",
        guard="reporting a timed-out operation as timed_out rather than cancelled",
        edits=(
            (
                PACKAGE / "execution.py",
                """            stopped = future.cancel()
            return result(
                CallState.TIMED_OUT,""",
                """            stopped = future.cancel()
            return result(
                CallState.CANCELLED,""",
            ),
        ),
    ),
    Mutation(
        name="slot-release-on-timeout",
        guard="holding the concurrency slot until the operation actually finishes",
        edits=(
            (
                PACKAGE / "execution.py",
                """            stopped = future.cancel()
            return result(""",
                """            stopped = future.cancel()
            release()
            return result(""",
            ),
        ),
    ),
    Mutation(
        name="cost-check-before-execution",
        guard="charging the cost budget before the capability runs",
        edits=(
            (
                PACKAGE / "execution.py",
                "        if scope.cost_spent + cost > scope.max_cost:",
                "        if scope.cost_spent > scope.max_cost + cost:",
            ),
        ),
    ),
    Mutation(
        name="cancellation-confirmation",
        guard="waiting for the grace period before claiming a cancellation succeeded",
        edits=(
            (
                PACKAGE / "execution.py",
                """        task.cancel()
        stopped, _ = await asyncio.wait({task}, timeout=self.cancellation_grace_s)
        if stopped:""",
                """        task.cancel()
        stopped = True
        if stopped:""",
            ),
        ),
    ),
    # -- Phase 2.5: integrity -------------------------------------------------
    Mutation(
        name="integrity-manifest-hash",
        guard="refusing a module whose bytes do not match the manifest's wasm_sha256",
        edits=(
            (
                RUNTIME,
                """        if self._require_integrity:
            if manifest.wasm_sha256 is None:""",
                """        if False:  # mutation: integrity requirement disabled
            if manifest.wasm_sha256 is None:""",
            ),
        ),
    ),
    Mutation(
        name="integrity-path-shape",
        guard="rejecting '..' in a manifest-declared module path",
        edits=(
            (
                PACKAGE / "integrity.py",
                """    if posix.parts and posix.parts[0] == "..":
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path must not traverse out of the plugin directory",
            plugin_id=plugin_id,
            path=raw,
        )
""",
                "",
            ),
            (
                PACKAGE / "integrity.py",
                """    if ".." in posix.parts:
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path must not contain '..' components",
            plugin_id=plugin_id,
            path=raw,
        )
""",
                "",
            ),
        ),
    ),
    Mutation(
        name="install-conflict",
        guard="refusing to overwrite a pin without an explicit replace",
        edits=(
            (
                PACKAGE / "integrity.py",
                """        if not replace:
            raise plugin_error(""",
                """        if False:  # mutation: silent overwrite
            raise plugin_error(""",
            ),
        ),
    ),
    # -- Phase 2.5: fairness --------------------------------------------------
    Mutation(
        name="queue-fairness",
        guard="rotating served plugins to the back of the round-robin order",
        edits=(
            (
                PACKAGE / "execution.py",
                "                self._rotate_locked(plugin_id)",
                "                pass  # mutation: strict arrival order, no rotation",
            ),
        ),
    ),
    Mutation(
        name="queue-bound",
        guard="refusing once the admission queue is full",
        edits=(
            (
                PACKAGE / "execution.py",
                "            elif self._waiter_count >= self.max_waiters:",
                "            elif False:  # mutation: unbounded queue",
            ),
        ),
    ),
    Mutation(
        name="rate-limit",
        guard="refusing a call that exceeds the per-plugin rate limit",
        edits=(
            (
                PACKAGE / "execution.py",
                """        if limit_per_s is None:
            return None""",
                """        if limit_per_s is None or True:  # mutation: no rate limiting
            return None""",
            ),
        ),
    ),
    Mutation(
        name="queue-refusal-not-charged",
        guard="charging only admitted calls, never a refused waiter",
        edits=(
            (
                PACKAGE / "execution.py",
                "        self._waiters.setdefault(plugin_id, deque()).append(waiter)",
                "        waiter.scope.cost_spent += waiter.cost  # mutation: charge on enqueue\n"
                "        self._waiters.setdefault(plugin_id, deque()).append(waiter)",
            ),
        ),
    ),
    Mutation(
        name="activation-approval",
        guard="refusing to instantiate a build that no current approval covers",
        edits=(
            (
                RUNTIME,
                "        if self._activation_guard is not None:",
                "        if False:  # mutation: activation guard disabled",
            ),
        ),
    ),
    Mutation(
        name="approval-escalation",
        guard="refusing a build whose manifest grew a capability after approval",
        edits=(
            (
                LIFECYCLE,
                "    if added:\n        return ApprovalCheck(",
                "    if False:  # mutation: escalation allowed\n        return ApprovalCheck(",
            ),
        ),
    ),
    Mutation(
        name="approval-content-binding",
        guard="binding an approval to the module digest, not just the plugin id",
        edits=(
            (
                LIFECYCLE,
                "    if approval.wasm_sha256 != digest:",
                "    if False:  # mutation: approval not bound to content",
            ),
        ),
    ),
    Mutation(
        name="grant-provenance",
        guard="requiring a grant or one-shot, not a policy default, to authorise a plugin call",
        edits=(
            (
                HOST,
                "        if verdict.via not in (DecisionVia.GRANT, DecisionVia.ONE_SHOT):",
                "        if False:  # mutation: any ALLOW is treated as authorisation",
            ),
        ),
    ),
    Mutation(
        name="revoke-pending-one-shot",
        guard="dropping in-memory one-shot allowances when grants are revoked",
        edits=(
            (
                MANAGER,
                "        for key in dropped:\n            self._one_shot.discard(key)",
                "        for key in ():  # mutation: one-shots survive revoke\n"
                "            self._one_shot.discard(key)",
            ),
        ),
    ),
    Mutation(
        name="reinstall-clears-approval",
        guard="clearing the approval when a plugin is re-pinned to a different build",
        edits=(
            (
                LIFECYCLE,
                "                    approval=None,\n"
                "                    previous_approval=existing.approval"
                " if existing is not None else None,",
                "                    approval=existing.approval"
                " if existing is not None else None,\n"
                "                    previous_approval=None,  # mutation: inherited",
            ),
        ),
    ),
    # -- Phase 2.6.2: consistency of the decision with what dispatch honours ----
    Mutation(
        name="install-idempotent",
        guard="doing nothing when the bytes being installed are already pinned",
        edits=(
            (
                LIFECYCLE,
                "            if mismatch is None:\n                return installed, existing",
                "            if False:  # mutation: every install rewrites the record\n"
                "                return installed, existing",
            ),
        ),
    ),
    Mutation(
        name="approval-narrowing",
        guard="revoking the grants a narrower approval no longer covers",
        edits=(
            (
                LIFECYCLE,
                "            stale = sorted(current - wanted)",
                "            stale = []  # mutation: an approval only ever adds",
            ),
        ),
    ),
    Mutation(
        name="revocation-drains-queue",
        guard="refusing calls that were queued before the approval was withdrawn",
        edits=(
            (
                PACKAGE / "execution.py",
                "                if self._remove_waiter_locked(waiter):\n"
                "                    queued_stopped += 1",
                "                if False:  # mutation: queued calls survive revocation\n"
                "                    queued_stopped += 1",
            ),
        ),
    ),
    Mutation(
        name="dispatch-approval",
        guard="requiring an approval, not just a grant, to authorise a plugin call",
        edits=(
            (
                HOST,
                "        if self.dispatch_guard is not None:",
                "        if False:  # mutation: a grant is treated as an approval",
            ),
        ),
    ),
    Mutation(
        name="one-shot-restored",
        guard="handing back a one-shot that admission refused",
        edits=(
            (
                HOST,
                "        settle(outcome.executed)",
                "        settle(True)  # mutation: always spent",
            ),
        ),
    ),
    Mutation(
        name="approval-grants-subset",
        guard="creating grants for the approved set, not for everything requested",
        edits=(
            (
                LIFECYCLE,
                "            for capability in missing:",
                "            for capability in sorted(requested):  # every request",
            ),
        ),
    ),
    # -- Phase 2.6.3: lifecycle integrity and audit corrections -----------------
    Mutation(
        name="canonical-manifest-path",
        guard="treating two spellings of one manifest path as one installation",
        edits=(
            (
                LIFECYCLE,
                "    return os.path.normcase(str(Path(path).resolve()))",
                "    return str(path)  # mutation: compare the spelling, not the file",
            ),
        ),
    ),
    Mutation(
        name="install-abi-version-identity",
        guard="not skipping the write when the ABI version moved under the same pin",
        edits=(
            (
                LIFECYCLE,
                "    if record.interface_version != manifest.interface_version:",
                "    if False:  # mutation: the ABI version is not part of identity",
            ),
        ),
    ),
    Mutation(
        name="guard-defect-is-a-refusal",
        guard="auditing a defect in the approval check as a refusal, not an execution",
        edits=(
            (
                HOST,
                "            except Exception as exc:\n"
                "                # A defect in the approval check is still a refusal, and it must",
                "            except ValueError as exc:  # mutation: a defect escapes dispatch\n"
                "                # A defect in the approval check is still a refusal, and it must",
            ),
        ),
    ),
    Mutation(
        name="guard-refusal-returns-one-shot",
        guard="handing back a one-shot that the approval boundary refused",
        edits=(
            (
                HOST,
                "                settle(False)\n"
                "                return done(\n"
                "                    HostCallStatus.DENIED_NO_GRANT,\n"
                '                    "not_approved",\n'
                "                    exc.message,",
                "                pass  # mutation: the one-shot stays reserved\n"
                "                return done(\n"
                "                    HostCallStatus.DENIED_NO_GRANT,\n"
                '                    "not_approved",\n'
                "                    exc.message,",
            ),
        ),
    ),
    Mutation(
        name="shared-revocation-barrier",
        guard="sharing the revocation block between lifecycles over one store",
        edits=(
            (
                LIFECYCLE,
                "        existing = _BARRIERS.get(key)\n"
                "        if existing is not None:\n"
                "            return existing",
                "        existing = None  # mutation: one barrier per lifecycle\n"
                "        if existing is not None:\n"
                "            return existing",
            ),
        ),
    ),
    Mutation(
        name="shared-memory-barrier",
        guard="sharing the revocation block when two lifecycles share an in-memory store",
        edits=(
            (
                LIFECYCLE,
                "                existing = _STORE_BARRIERS.get(store)\n"
                "                if existing is not None:\n"
                "                    return existing",
                "                existing = None  # mutation: one barrier per instance\n"
                "                if existing is not None:\n"
                "                    return existing",
            ),
        ),
    ),
    Mutation(
        name="no-stale-lift",
        guard="refusing to lift a block when a revocation landed after the record was read",
        edits=(
            (
                PACKAGE / "execution.py",
                "            if self._revocations.get(plugin_id, 0) != observed:",
                "            if False:  # mutation: a stale approval may lift the block",
            ),
        ),
    ),
    Mutation(
        name="expired-grants-are-not-authority",
        guard="counting only unexpired grants as current authority",
        edits=(
            (
                LIFECYCLE,
                "        return [row for row in await raw() if not _grant_expired(row)], True",
                "        return list(await raw()), True  # mutation: expired rows count",
            ),
            (
                MANAGER,
                "        return [\n"
                "            row for row in await self.list_grants() "
                'if not grant_is_expired(row.get("expires_at"))\n'
                "        ]",
                "        return await self.list_grants()  # mutation: expired rows count",
            ),
        ),
    ),
    Mutation(
        name="approval-store-failure-is-structured",
        guard="reporting a store failure while saving an approval as a lifecycle error",
        edits=(
            (
                LIFECYCLE,
                "            except Exception as exc:\n"
                "                # Not transactional, and not described as if it were: the grants",
                "            except ValueError as exc:  # mutation: the OSError escapes approve\n"
                "                # Not transactional, and not described as if it were: the grants",
            ),
        ),
    ),
    # -- Phase 2.6.6: withdrawal ownership, closure, uninstall order, lock -----
    Mutation(
        name="withdrawal-token-ownership",
        guard="only the token that placed a withdrawal mark can end it",
        edits=(
            (
                PACKAGE / "execution.py",
                "            marks = self._withdrawing.get(withdrawal.plugin_id)\n"
                "            if not marks or withdrawal not in marks:\n"
                "                return False",
                "            marks = self._withdrawing.get(withdrawal.plugin_id)\n"
                "            if True:  # mutation: one end clears every mark for the plugin\n"
                "                self._withdrawing.pop(withdrawal.plugin_id, None)\n"
                "                return True\n"
                "            if not marks or withdrawal not in marks:\n"
                "                return False",
            ),
        ),
    ),
    Mutation(
        name="withdrawal-mark-vetoes-both-lifts",
        guard="both lift paths refusing while a withdrawal mark is set",
        edits=(
            (
                PACKAGE / "execution.py",
                "            if self._withdrawing.get(plugin_id):\n"
                "                return False\n"
                "            self._blocked.discard(plugin_id)",
                "            if False:  # mutation: a mark no longer vetoes an unconditional lift\n"
                "                return False\n"
                "            self._blocked.discard(plugin_id)",
            ),
            (
                PACKAGE / "execution.py",
                "            if self._withdrawing.get(plugin_id):\n"
                "                return False\n"
                "            if self._revocations.get(plugin_id, 0) != observed:",
                "            if False:  # mutation: a mark no longer vetoes the conditional lift\n"
                "                return False\n"
                "            if self._revocations.get(plugin_id, 0) != observed:",
            ),
        ),
    ),
    Mutation(
        name="closed-lifecycle-still-writes",
        guard="a closed lifecycle refusing every entry point that writes",
        edits=(
            (
                LIFECYCLE,
                "            self._closed = True\n            idle = self._inflight == 0",
                "            self._closed = False  # mutation: still an authority\n"
                "            idle = self._inflight == 0",
            ),
        ),
    ),
    Mutation(
        name="close-releases-lock-while-in-flight",
        guard="holding the store lock until the last in-flight operation returns",
        edits=(
            (
                LIFECYCLE,
                "        if idle:\n            self._release_store_lock()",
                "        if True:  # mutation: release before the writers are done\n"
                "            self._release_store_lock()",
            ),
        ),
    ),
    Mutation(
        name="aclose-does-not-wait-for-in-flight-work",
        guard="aclose() waiting for in-flight work before it returns",
        edits=(
            (
                LIFECYCLE,
                "        self.close()\n        await self._wait_for_drain()\n        try:",
                "        self.close()\n        try:  # mutation: no wait",
            ),
        ),
    ),
    Mutation(
        name="uninstall-deletes-any-pin",
        guard="deleting the pin only when it still describes this install",
        edits=(
            (
                LIFECYCLE,
                "        if not isinstance(raw, dict) or (\n"
                '            raw.get("plugin_id") != plugin_id or '
                'raw.get("wasm_sha256") != record.wasm_sha256\n'
                "        ):",
                "        if False:  # mutation: whatever is at the path is ours to delete",
            ),
        ),
    ),
    Mutation(
        name="uninstall-follows-a-pin-symlink",
        guard="refusing to delete a pin that is a symbolic link",
        edits=(
            (
                LIFECYCLE,
                "            if pin_path.is_symlink():",
                "            if False:  # mutation: the link is a pin like any other",
            ),
        ),
    ),
    Mutation(
        name="uninstall-cannot-be-retried",
        guard="re-running an uninstall that recorded uninstalled as a cleanup",
        edits=(
            (
                LIFECYCLE,
                "        {InstallState.INSTALLED, InstallState.DISCOVERED, "
                "InstallState.UNINSTALLED}",
                "        {InstallState.INSTALLED, InstallState.DISCOVERED}  # mutation: no retry",
            ),
        ),
    ),
    Mutation(
        name="closed-lifecycle-still-vouches",
        guard="a closed lifecycle refusing to authorise a dispatch or a load",
        edits=(
            (
                LIFECYCLE,
                "        if self._closed:\n"
                "            # A closed lifecycle has released the store, so it cannot see a",
                "        if False:  # mutation: still vouches\n"
                "            # A closed lifecycle has released the store, so it cannot see a",
            ),
        ),
    ),
    Mutation(
        name="uninstall-store-failure-is-structured",
        guard="reporting a failed uninstall record write as a lifecycle error",
        edits=(
            (
                LIFECYCLE,
                "                except Exception as exc:\n"
                "                    # The decision could not be stored, so it was not taken: a\n"
                "                    # failed uninstall must not have half-happened.",
                "                except ValueError as exc:  # mutation: escapes\n"
                "                    # The decision could not be stored, so it was not taken: a\n"
                "                    # failed uninstall must not have half-happened.",
            ),
        ),
    ),
    Mutation(
        name="uninstall-pin-before-record",
        guard="the pin file surviving a failed uninstall record write",
        edits=(
            (
                LIFECYCLE,
                "                try:\n                    uninstalled = await self._save(",
                "                if record.manifest_path:  # mutation: pin first\n"
                "                    Path(record.manifest_path).parent.joinpath(\n"
                "                        INSTALL_RECORD_NAME\n"
                "                    ).unlink(missing_ok=True)\n"
                "                try:\n                    uninstalled = await self._save(",
            ),
        ),
    ),
    Mutation(
        name="lock-create-failure-is-a-conflict",
        guard="reporting a lock file that cannot be created as broken, not busy",
        edits=(
            (
                LOCKING,
                "        # tell the caller to wait for a process that does not exist.\n"
                "        raise _broken(store_path, lock_path, exc) from exc",
                "        # tell the caller to wait for a process that does not exist.\n"
                "        raise _busy(store_path, lock_path, exc) from exc  # mutation",
            ),
        ),
    ),
    Mutation(
        name="lock-contention-is-a-broken-lock",
        guard="reporting contention as plugin_store_locked",
        edits=(
            (
                LOCKING,
                "            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
                '            return "msvcrt"\n'
                "        except OSError as exc:\n"
                "            os.close(fd)\n"
                "            if exc.errno in _BUSY:\n"
                "                raise _busy(store_path, lock_path, exc) from exc",
                "            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
                '            return "msvcrt"\n'
                "        except OSError as exc:\n"
                "            os.close(fd)\n"
                "            if exc.errno in _BUSY:\n"
                "                raise _broken(store_path, lock_path, exc) from exc  # mutation",
            ),
        ),
    ),
    Mutation(
        name="lock-failure-leaks-descriptor",
        guard="closing the lock descriptor when the OS lock cannot be taken",
        edits=(
            (
                LOCKING,
                '            return "msvcrt"\n'
                "        except OSError as exc:\n"
                "            os.close(fd)",
                '            return "msvcrt"\n        except OSError as exc:\n'
                "            pass  # mutation: the descriptor is leaked",
            ),
        ),
    ),
    Mutation(
        name="guard-installed-before-the-lock",
        guard="installing dispatch_guard only after the store lock and host exist",
        edits=(
            (
                LIFECYCLE,
                "        self._revoking: set[str] = set()",
                "        self._revoking: set[str] = set()\n"
                "        registry.dispatch_guard = self._dispatch_guard  # mutation: too early",
            ),
        ),
    ),
    # -- Phase 2.6.8b: the three recovery contracts --------------------------
    Mutation(
        name="revocation-cannot-be-retried",
        guard="re-running a revocation that recorded revoked as a cleanup (C1)",
        edits=(
            (
                LIFECYCLE,
                """    InstallState.REVOKED: frozenset(
        {InstallState.APPROVED, InstallState.UNINSTALLED, InstallState.REVOKED}
    ),""",
                """    InstallState.REVOKED: frozenset(  # mutation: no retry
        {InstallState.APPROVED, InstallState.UNINSTALLED}
    ),""",
            ),
        ),
    ),
    Mutation(
        name="revoke-grants-failure-escapes",
        guard="classifying a failed grant withdrawal as plugin_lifecycle (C2)",
        edits=(
            (
                LIFECYCLE,
                """                except Exception as exc:
                    # The record says ``revoked`` and the block is set, so a""",
                """                except ValueError as exc:  # mutation: the failure escapes raw
                    # The record says ``revoked`` and the block is set, so a""",
            ),
        ),
    ),
    Mutation(
        name="empty-approval-lifts-the-block",
        guard="an approval covering no capability not lifting a revocation block (C3)",
        edits=(
            (
                LIFECYCLE,
                """            if wanted:
                self._host.executor.allow_plugin(plugin_id)""",
                """            if wanted or True:  # mutation: an empty decision lifts it too
                self._host.executor.allow_plugin(plugin_id)""",
            ),
        ),
    ),
    # -- Phase 2.6.9b: aclose() reliability -----------------------------------
    Mutation(
        name="drain-wakeup-ignores-the-waiter-loop",
        guard="waking a drain waiter on the loop that owns it",
        edits=(
            (
                LIFECYCLE,
                "            waiter.get_loop().call_soon_threadsafe(_resolve_waiter, waiter)",
                "            _resolve_waiter(waiter)  # mutation: completed from the wrong thread",
            ),
        ),
    ),
    Mutation(
        name="aclose-host-failure-escapes",
        guard="classifying a failed host shutdown as plugin_lifecycle",
        edits=(
            (
                LIFECYCLE,
                """        except PluginError:
            raise
        except Exception as exc:
            # ``close()`` has already run, so the store lock is released and the""",
                """        except PluginError:
            raise
        except ValueError as exc:  # mutation: the host failure escapes raw
            # ``close()`` has already run, so the store lock is released and the""",
            ),
        ),
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise(raw: bytes) -> tuple[str, bool]:
    """Return (LF text, whether the file used CRLF).

    Multi-line anchors are written with LF. The working tree on Windows is CRLF,
    so matching has to happen on a normalised copy — while the *restore* stays
    byte-exact, because that is what makes the check trustworthy.
    """
    text = raw.decode("utf-8")
    return text.replace("\r\n", "\n"), b"\r\n" in raw


def _apply(raw: bytes, old: str, new: str) -> bytes:
    text, crlf = _normalise(raw)
    mutated = text.replace(old, new, 1)
    if crlf:
        mutated = mutated.replace("\n", "\r\n")
    return mutated.encode("utf-8")


def _run_tests() -> bool:
    """True when the suite passes, False when it fails (i.e. the mutation was caught)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", *TEST_TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def main() -> int:
    if not _run_tests():
        print("baseline is already failing — fix that before trusting this check")
        return 2

    caught: list[str] = []
    survived: list[str] = []
    skipped: list[str] = []

    for mutation in MUTATIONS:
        originals = {path: path.read_bytes() for path, _, _ in mutation.edits}
        digests = {path: hashlib.sha256(data).hexdigest() for path, data in originals.items()}

        applicable = True
        for path, old, _ in mutation.edits:
            normalised, _crlf = _normalise(originals[path])
            count = normalised.count(old)
            if count != 1:
                print(f"SKIPPED   {mutation.name}: anchor in {path.name} matched {count} times")
                skipped.append(mutation.name)
                applicable = False
                break
        if not applicable:
            continue

        intact = True
        passed = False
        try:
            for path, old, new in mutation.edits:
                path.write_bytes(_apply(path.read_bytes(), old, new))
            passed = _run_tests()
        finally:
            for path, data in originals.items():
                path.write_bytes(data)
                if _sha256(path) != digests[path]:
                    print(f"RESTORE FAILED for {path} — stop and inspect the working tree")
                    intact = False

        # Outside the finally on purpose: a `return` in a `finally` block swallows
        # whatever exception the try block was raising, which would hide a crashed
        # test run as a tidy "restore failed".
        if not intact:
            return 3

        if passed:
            print(f"SURVIVED  {mutation.name}: {mutation.guard}")
            survived.append(mutation.name)
        else:
            print(f"caught    {mutation.name}: {mutation.guard}")
            caught.append(mutation.name)

    total = len(MUTATIONS)
    print()
    print(f"{len(caught)}/{total} caught, {len(survived)} survived, {len(skipped)} skipped")
    if survived or skipped:
        print("NOT PROVEN: " + ", ".join(survived + skipped))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
