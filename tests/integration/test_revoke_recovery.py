"""Phase 2.6.8a: the three recovery contracts the 2.6.8 audit found missing.

Tests only — this file is the specification of what the next phase has to make
true, written before the change so the red is attributable rather than assumed.
Each test names the contract it pins:

* **C1** — a revocation whose record is already persisted as ``revoked`` but whose
  grant withdrawal failed can be **retried**, and the retry finishes the cleanup
  without granting anything and without lifting the block;
* **C2** — that same failure is reported as the repository's structured error
  (``plugin_lifecycle`` with ``revocation_persisted``/``grants_withdrawn``), the
  way ``uninstall()`` already reports it;
* **C3** — an approval that covers no capability must not lift the revocation
  block.

How the failure is injected: ``GatedAuthority`` (the existing fixture from
``tests/support/inflight.py``) parks ``revoke()`` until ``release()`` is called,
so the gate is opened up front here and the injected exception is what the call
meets. ``fail_with`` is cleared for the retry, which is the whole point — the
second call runs against the state the first one left behind.

What this does **not** claim: cross-process restart. Each test retries on the
same ``PluginLifecycle`` instance, which models an operator re-running the verb
that failed. A real restart loses the in-memory barrier, and that case needs a
second process; it is out of scope here on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deskweave_plugin_wasm.errors import PluginLifecycleError, PluginNotApprovedError
from deskweave_plugin_wasm.host import default_registry
from deskweave_plugin_wasm.integrity import sha256_file
from deskweave_plugin_wasm.lifecycle import InstallState, JsonApprovalStore, PluginLifecycle
from support import wasm_plugins as fixtures
from support.inflight import GatedAuthority

VALID = "reference.metadata.validate"
PLUGIN = "revoke-recovery"
PRINCIPAL = f"wasm-plugin:{PLUGIN}"


def _manifest(directory: Path) -> Path:
    """A real WAT module plus the manifest that describes its bytes."""
    request = json.dumps(
        {
            "capability": VALID,
            "payload": {"image": {"artifact_id": "a", "title": "t", "width": 1, "height": 1}},
        }
    )
    module = fixtures.write_plugin(
        directory, fixtures.host_request(request), name=PLUGIN, manifest_dict={}
    )
    digest, _size = sha256_file(module)
    payload = fixtures.example_manifest(
        plugin_id=PLUGIN,
        wasm_path=module.name,
        wasm_sha256=digest,
        version="1.0.0",
        requested_capabilities=[VALID],
        input_schema={},
        output_schema={},
    )
    manifest = module.with_suffix(".json")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


async def _approved(directory: Path) -> tuple[PluginLifecycle, GatedAuthority, Path]:
    """An installed, approved plugin whose grants exist in the authority."""
    authority = GatedAuthority()
    authority.release()  # no parking here: these tests are about the failure, not the gate
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None),
        permissions=authority,
        store=JsonApprovalStore(directory / "approvals.json"),
    )
    manifest = _manifest(directory)
    await lifecycle.install(manifest)
    await lifecycle.approve(PLUGIN, [VALID])
    return lifecycle, authority, directory / "approvals.json"


def _record(store: Path) -> dict[str, Any]:
    return json.loads(store.read_text(encoding="utf-8"))["plugins"][PLUGIN]


async def _failed_revocation(
    lifecycle: PluginLifecycle, authority: GatedAuthority
) -> BaseException:
    """Drive a revocation that persists, then fails at the grants step.

    Returns the exception instead of asserting on its type: C2 is what decides the
    type, and a test that demanded ``PluginLifecycleError`` here would fail for the
    wrong reason until then.
    """
    authority.fail_with = RuntimeError("test: the grants store is down")
    try:
        await lifecycle.revoke(PLUGIN)
    except BaseException as exc:
        return exc
    finally:
        authority.fail_with = None
    raise AssertionError("the injected grants failure did not surface at all")


# -- C1: the retry ------------------------------------------------------------


async def test_a_revocation_that_failed_at_the_grants_step_can_be_retried(
    tmp_path: Path,
) -> None:
    """The record says revoked and the grants could not be withdrawn. Retry finishes it.

    State that must survive the failed attempt: the record (``revoked``), the
    block, and the residual grant — the last one is not a bug, it is the reason a
    retry is needed. The retry must then remove the grant without creating one and
    without lifting the block.
    """
    lifecycle, authority, store = await _approved(tmp_path)
    try:
        await _failed_revocation(lifecycle, authority)

        assert _record(store)["state"] == "revoked", "the decision must be persisted first"
        assert authority.granted == {PRINCIPAL: {VALID}}, (
            "a failure at the grants step must leave the grant for the retry to remove; "
            f"found {authority.granted}"
        )
        assert lifecycle.host.executor.is_revoked(PLUGIN), (
            "a revocation that was persisted must keep the block, even though the "
            "grants withdrawal failed"
        )
        grants_before = authority.grant_calls

        retry_error: BaseException | None = None
        report = None
        try:
            report = await lifecycle.revoke(PLUGIN)
        except BaseException as exc:
            retry_error = exc
        assert retry_error is None, (
            "a revocation that already recorded 'revoked' must be retryable as a "
            f"cleanup; the retry was refused: {retry_error}"
        )
        assert report is not None
        assert report.revoked_grants, "the retry must actually withdraw the residual grant"
        assert authority.granted == {}, f"the retry left {authority.granted} behind"
        assert _record(store)["state"] == "revoked", "a cleanup must not change the decision"
        assert lifecycle.host.executor.is_revoked(PLUGIN), (
            "a cleanup has no authority to lift the block; only a new approval does"
        )
        assert authority.grant_calls == grants_before, (
            "recovery must never create a grant: a retry is a withdrawal, not a decision"
        )
    finally:
        lifecycle.close()


# -- C2: the failure is classified -------------------------------------------


async def test_a_grants_failure_is_reported_as_a_structured_lifecycle_error(
    tmp_path: Path,
) -> None:
    """Same convention ``uninstall()`` already uses, on the same two facts.

    A caller has to be able to tell "the decision was stored, the grants were not
    withdrawn" from "nothing happened". Without that, the only safe response to
    the exception is to stop and inspect the store by hand — which is exactly what
    the audit had to do.
    """
    lifecycle, authority, store = await _approved(tmp_path)
    try:
        authority.fail_with = RuntimeError("test: the grants store is down")
        raised: BaseException | None = None
        try:
            await lifecycle.revoke(PLUGIN)
        except BaseException as exc:
            raised = exc
        finally:
            authority.fail_with = None

        assert raised is not None, "the injected failure did not surface"
        assert isinstance(raised, PluginLifecycleError), (
            "a failed grants withdrawal must be reported as plugin_lifecycle, not "
            f"{type(raised).__name__}: the caller cannot tell it apart from a bug"
        )
        assert raised.code == "plugin_lifecycle"
        assert raised.details["revocation_persisted"] is True, (
            "the record is already on disk; saying otherwise invites a caller to "
            "believe the plugin still has its approval"
        )
        assert raised.details["grants_withdrawn"] is False
        assert raised.details["plugin_id"] == PLUGIN
        assert isinstance(raised.__cause__, RuntimeError), (
            "the original failure must be chained, as every other structured "
            f"error in this package does; found {type(raised.__cause__).__name__}"
        )
        assert _record(store)["state"] == "revoked"
    finally:
        lifecycle.close()


async def test_a_persistence_failure_is_already_structured_with_the_other_half(
    tmp_path: Path,
) -> None:
    """Control: the same method already reports the mirror case this way.

    If this passes while the grants case above fails, the gap is narrow and named:
    ``revoke()`` knows how to say ``revocation_persisted=False``; it does not know
    how to say ``revocation_persisted=True, grants_withdrawn=False``.

    (The same shape is asserted from the uninstall side by
    ``test_uninstall_failure.py::test_a_failed_grant_withdrawal_is_reported_not_swallowed``;
    this one keeps the pair inside one file so the asymmetry is visible.)
    """

    class FailingStore(JsonApprovalStore):
        fail_next = False

        async def save(self, record: Any) -> None:
            if self.fail_next:
                self.fail_next = False
                raise OSError("test: the approval store is down")
            await super().save(record)

    authority = GatedAuthority()
    authority.release()
    store = FailingStore(tmp_path / "approvals.json")
    lifecycle = PluginLifecycle(
        registry=default_registry(None, None), permissions=authority, store=store
    )
    try:
        manifest = _manifest(tmp_path)
        await lifecycle.install(manifest)
        await lifecycle.approve(PLUGIN, [VALID])

        store.fail_next = True
        with pytest.raises(PluginLifecycleError) as caught:
            await lifecycle.revoke(PLUGIN)

        assert caught.value.details["revocation_persisted"] is False
        assert _record(tmp_path / "approvals.json")["state"] == "approved", (
            "a decision that could not be stored must not appear in the record"
        )
        assert authority.granted == {PRINCIPAL: {VALID}}, "nothing may be withdrawn yet"
        assert not lifecycle.host.executor.is_revoked(PLUGIN), (
            "the block this call placed is its own, and must be removed again when "
            "the decision was not taken"
        )
    finally:
        lifecycle.close()


# -- C3: the empty approval ---------------------------------------------------


async def test_an_empty_approval_does_not_clear_the_revocation_block(
    tmp_path: Path,
) -> None:
    """C3: approving nothing must not make a revoked plugin admissible again.

    Whether ``approve(plugin, [])`` should be *rejected* is not defined anywhere
    in this repository (the state machine permits ``revoked -> approved``, and no
    document or test says an empty capability set is illegal), so this does not
    assert that it raises. It asserts the one thing that is not negotiable: an
    approval that authorises no capability must not lift a block that a
    revocation put in place. See the report for the ambiguity itself.

    Observable consequence, asserted separately because it is the part that is
    already true: the previously approved capability still cannot run.
    """
    lifecycle, authority, _store = await _approved(tmp_path)
    try:
        await lifecycle.revoke(PLUGIN)
        assert lifecycle.host.executor.is_revoked(PLUGIN), "precondition: the block is set"
        assert authority.granted == {}, "precondition: the revocation withdrew the grants"

        await lifecycle.approve(PLUGIN, [])

        assert lifecycle.host.executor.is_revoked(PLUGIN), (
            "an approval with no capabilities authorises nothing, so it must not be "
            "able to lift the block a revocation placed"
        )
        with pytest.raises(PluginNotApprovedError):
            await lifecycle._dispatch_guard(PLUGIN, VALID)
    finally:
        lifecycle.close()


async def test_a_non_empty_approval_does_clear_the_revocation_block(tmp_path: Path) -> None:
    """Control: the same call with a capability *does* lift it, so the test above
    is not passing because nothing ever lifts the block.

    A person changing their mind is the one authority that can undo a revocation;
    that is the path this control exercises.
    """
    lifecycle, authority, _store = await _approved(tmp_path)
    try:
        await lifecycle.revoke(PLUGIN)
        assert lifecycle.host.executor.is_revoked(PLUGIN), "precondition: the block is set"

        await lifecycle.approve(PLUGIN, [VALID])

        assert not lifecycle.host.executor.is_revoked(PLUGIN), (
            "re-approving a capability is a new decision and must make the plugin admissible again"
        )
        await lifecycle._dispatch_guard(PLUGIN, VALID)
        assert authority.granted == {PRINCIPAL: {VALID}}, (
            "the new approval must create the grant it authorises"
        )
    finally:
        lifecycle.close()


# -- what the recovery must not disturb --------------------------------------


async def test_a_retried_revocation_keeps_the_tombstone_semantics_of_a_later_uninstall(
    tmp_path: Path,
) -> None:
    """After a retried revocation the plugin is still uninstallable, and the pin goes.

    A cleanup edge must not close the door on the verb that finishes the job: if
    ``revoked -> revoked`` were implemented by writing something else (``install``
    or ``approved``), ``uninstall()`` would refuse and the plugin could not be
    removed at all.
    """
    lifecycle, authority, store = await _approved(tmp_path)
    try:
        await _failed_revocation(lifecycle, authority)
        retry_error: BaseException | None = None
        try:
            await lifecycle.revoke(PLUGIN)  # the retry (C1)
        except BaseException as exc:
            retry_error = exc
        assert retry_error is None, (
            "the retry must be accepted, or 'uninstall' becomes the only way out and "
            f"it deletes the pin: {retry_error}"
        )

        record = await lifecycle.uninstall(PLUGIN)

        assert record.state is InstallState.UNINSTALLED
        assert _record(store)["state"] == "uninstalled"
        assert authority.granted == {}
        assert not (tmp_path / "deskweave.install.json").is_file(), (
            "uninstall still owns the pin; the revocation retry must not have "
            "left a second code path that keeps it"
        )
    finally:
        lifecycle.close()
