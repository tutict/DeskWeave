"""Recovery of tasks that a previous process left non-terminal.

The crash is simulated faithfully but cheaply: the failure is raised as a
``BaseException`` *inside* the running coroutine, exactly where a killed process
would stop. Nothing is cleaned up — that is the point — so the database keeps the
status and artifacts a hard kill leaves behind. Recovery then runs against a
**fresh runtime on the same data directory**, which is the same sequence as
restarting the desktop app.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deskweave_contracts import (
    ArtifactKind,
    CollectionRequest,
    ConfirmRequest,
    TaskRecord,
    TaskStatus,
)
from deskweave_runtime import Runtime, RuntimeConfig
from deskweave_runtime.core import HealthState
from support.doubles import (
    FakeBlender,
    FakeBrowser,
    FakeDownloader,
    SimulatedCrash,
    candidates,
)

WORKFLOW = "reference-collection"


async def _first_runtime(
    config: RuntimeConfig,
) -> tuple[Runtime, FakeBrowser, FakeBlender]:
    """A runtime wired like production, with fake transports."""
    runtime = await Runtime.create(config)
    await runtime.start()
    runtime.artifacts._downloader = FakeDownloader(config.data_dir / "artifacts")  # type: ignore[attr-defined]
    browser = FakeBrowser()
    blender = FakeBlender(config.data_dir / "artifacts")
    await runtime.register_adapter(browser)
    await runtime.register_adapter(blender)
    return runtime, browser, blender


async def _restarted(
    config: RuntimeConfig, *, blender: FakeBlender | None = None
) -> tuple[Runtime, FakeBlender, list[Any]]:
    """A fresh runtime on the same data dir, with adapters registered, then recovery."""
    runtime = await Runtime.create(config)
    await runtime.start()
    runtime.artifacts._downloader = FakeDownloader(config.data_dir / "artifacts")  # type: ignore[attr-defined]
    await runtime.register_adapter(FakeBrowser())
    adapter = blender or FakeBlender(config.data_dir / "artifacts")
    await runtime.register_adapter(adapter)
    outcomes = await runtime.recover()
    return runtime, adapter, outcomes


async def _wait_until_background_stops(runtime: Runtime, *, timeout: float = 10.0) -> None:
    """Wait for the in-flight collection coroutine to be gone (it never completes cleanly)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while runtime.references._background and loop.time() < deadline:  # type: ignore[attr-defined]
        await asyncio.sleep(0.01)
    assert not runtime.references._background, "background collection did not stop"  # type: ignore[attr-defined]


def _die_on_call(original: Any, *, call: int) -> Any:
    """Wrap an async method so its ``call``-th invocation raises a hard failure."""
    seen = {"n": 0}

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == call:
            raise asyncio.CancelledError(f"simulated hard kill on import #{call}")
        return await original(*args, **kwargs)

    return wrapper


async def _collect(runtime: Runtime, browser: FakeBrowser, limit: int = 2) -> TaskRecord:
    browser.candidates = candidates(limit)
    task = await runtime.references.start_collection(
        CollectionRequest(query="elf armor reference", limit=limit)
    )
    return await runtime.references.wait_until_settled(task.task_id)


# -- interrupted collection ------------------------------------------------
async def test_interrupted_collection_keeps_its_downloads(config: RuntimeConfig) -> None:
    """A kill mid-download must keep what was already saved, not discard it."""
    runtime, browser, _blender = await _first_runtime(config)
    browser.candidates = candidates(3)
    runtime.artifacts.import_image = _die_on_call(  # type: ignore[method-assign]
        runtime.artifacts.import_image, call=3
    )

    task = await runtime.references.start_collection(
        CollectionRequest(query="elf armor reference", limit=3)
    )
    await _wait_until_background_stops(runtime)

    interrupted = await runtime.workflow.get_task(task.task_id)
    assert interrupted.status is TaskStatus.RUNNING
    # The request was persisted before any work started.
    assert interrupted.result["request"] == {
        "query": "elf armor reference",
        "limit": 3,
        "source": "bing",
    }
    # The candidate list is only written once collection finishes.
    assert "candidates" not in interrupted.result
    await runtime.close()

    fresh, _adapter, outcomes = await _restarted(config)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.WAITING_FOR_CONFIRMATION
        recovery = recovered.result["recovery"]
        assert recovery["action"] == "resumed_partial_collection"
        assert "2 downloaded image(s)" in recovery["detail"]
        assert len(recovered.result["candidates"]) == 2
        assert recovered.result["failures"] == []

        view = await fresh.references.get_view(task.task_id)
        assert [artifact.artifact_id for artifact in view.artifacts] == (
            recovered.result["candidates"]
        )
        for artifact in view.artifacts:
            assert (await fresh.artifacts.resolve_path(artifact.artifact_id)).is_file()

        assert [outcome.action for outcome in outcomes] == ["resumed_partial_collection"]
        # The run stays open: the task is waiting for a human, not finished.
        assert (await fresh.workflow.get_run(task.run_id)).status.value == "running"
    finally:
        await fresh.close()


async def test_interrupted_collection_without_downloads_fails_explicitly(
    config: RuntimeConfig,
) -> None:
    runtime, browser, _ = await _first_runtime(config)
    browser.candidates = candidates(2)
    runtime.artifacts.import_image = _die_on_call(  # type: ignore[method-assign]
        runtime.artifacts.import_image, call=1
    )

    task = await runtime.references.start_collection(CollectionRequest(query="empty shell"))
    await _wait_until_background_stops(runtime)
    assert (await runtime.workflow.get_task(task.task_id)).status is TaskStatus.RUNNING
    await runtime.close()

    fresh, _adapter, outcomes = await _restarted(config)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.FAILED
        assert recovered.error is not None
        assert recovered.error.code == "interrupted"
        assert "start a new collection" in recovered.error.message
        assert recovered.result["recovery"]["action"] == "collection_interrupted"
        assert (await fresh.workflow.get_run(task.run_id)).status.value == "failed"
        assert [outcome.status for outcome in outcomes] == [TaskStatus.FAILED]
    finally:
        await fresh.close()


async def test_pending_task_is_failed_as_never_started(config: RuntimeConfig) -> None:
    """A crash between "task created" and "task running" leaves a pending row."""
    runtime, _, _ = await _first_runtime(config)
    run = await runtime.workflow.start_run(WORKFLOW)
    task = await runtime.workflow.create_task(run.run_id, "Collect reference images: never ran")
    assert task.status is TaskStatus.PENDING
    await runtime.close()

    fresh, _adapter, outcomes = await _restarted(config)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.FAILED
        assert recovered.error is not None and recovered.error.code == "interrupted"
        assert recovered.result["recovery"]["action"] == "never_started"
        assert [outcome.action for outcome in outcomes] == ["never_started"]
    finally:
        await fresh.close()


# -- interrupted confirmation ---------------------------------------------
async def test_interrupted_confirmation_uses_the_verified_file(config: RuntimeConfig) -> None:
    """The scene was written before the kill: verify it instead of running Blender again."""
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=2)
    selected = task.result["candidates"]

    blender.crash_after_write = True
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(
            task.task_id, ConfirmRequest(artifact_ids=selected, plane_name="ElfArmor")
        )

    interrupted = await runtime.workflow.get_task(task.task_id)
    assert interrupted.status is TaskStatus.RUNNING
    assert interrupted.result["confirmation"]["plane_name"] == "ElfArmor"
    assert "execution" not in interrupted.result
    assert blender.calls == [selected]
    await runtime.close()

    fresh, fresh_blender, outcomes = await _restarted(config)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.SUCCEEDED
        assert recovered.result["recovery"]["action"] == "verified_output"
        execution = recovered.result["execution"]
        assert execution["ok"] is True
        assert execution["recovered"] is True
        assert execution["verification"]["verified_objects"] == 2
        assert fresh_blender.calls == [], "recovery must never re-run the target"
        assert len(fresh_blender.verified) == 1

        blend_id = execution["blend_artifact_id"]
        artifact = await fresh.artifacts.get(blend_id)
        assert artifact.kind is ArtifactKind.MODEL
        assert artifact.task_id == task.task_id
        assert (await fresh.workflow.get_run(task.run_id)).status.value == "succeeded"
        assert [outcome.action for outcome in outcomes] == ["verified_output"]
    finally:
        await fresh.close()


async def test_interrupted_confirmation_with_unverifiable_file_asks_again(
    config: RuntimeConfig,
) -> None:
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=1)
    selected = task.result["candidates"]
    blender.crash_after_write = True
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    await runtime.close()

    corrupt = FakeBlender(config.data_dir / "artifacts", verify_error="scene is truncated")
    fresh, fresh_blender, outcomes = await _restarted(config, blender=corrupt)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert recovered.result["recovery"]["action"] == "confirmation_required"
        assert "did not verify" in recovered.result["recovery"]["detail"]
        assert fresh_blender.calls == []
        # No artifact was registered for a file that could not be verified.
        kinds = {artifact.kind for artifact in await fresh.artifacts.list_for_task(task.task_id)}
        assert ArtifactKind.MODEL not in kinds

        # The user can still retry — but only by confirming explicitly.
        fresh_blender.verify_error = None
        final = await fresh.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
        assert final.status is TaskStatus.SUCCEEDED
        assert len(fresh_blender.calls) == 1
        assert [outcome.action for outcome in outcomes] == ["confirmation_required"]
    finally:
        await fresh.close()


async def test_interrupted_confirmation_without_any_output_asks_again(
    config: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=1)
    selected = task.result["candidates"]

    async def _die_before_writing(_request: Any) -> dict[str, Any]:
        raise SimulatedCrash("simulated kill before the target ran")

    monkeypatch.setattr(blender, "_run", _die_before_writing)
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    await runtime.close()

    fresh, fresh_blender, _outcomes = await _restarted(config)
    try:
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert recovered.result["recovery"]["action"] == "confirmation_required"
        assert "no output file" in recovered.result["recovery"]["detail"]
        assert fresh_blender.calls == []
        assert fresh_blender.verified == []
        # The selection is still there, so the user can confirm again.
        assert recovered.result["confirmation"]["artifact_ids"] == selected
    finally:
        await fresh.close()


async def test_interrupted_confirmation_without_target_adapter_asks_again(
    config: RuntimeConfig,
) -> None:
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=1)
    selected = task.result["candidates"]
    blender.crash_after_write = True
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    await runtime.close()

    fresh = await Runtime.create(config)
    await fresh.start()
    fresh.artifacts._downloader = FakeDownloader(config.data_dir / "artifacts")  # type: ignore[attr-defined]
    await fresh.register_adapter(FakeBrowser())  # no Blender installed
    try:
        outcomes = await fresh.recover()
        recovered = await fresh.workflow.get_task(task.task_id)
        assert recovered.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert recovered.result["recovery"]["action"] == "confirmation_required"
        assert "not available" in recovered.result["recovery"]["detail"]
        assert [outcome.action for outcome in outcomes] == ["confirmation_required"]
    finally:
        await fresh.close()


# -- safety properties ----------------------------------------------------
async def test_unconfirmed_task_is_never_executed_by_recovery(config: RuntimeConfig) -> None:
    """The one thing recovery must never do: act without a human."""
    runtime, browser, _blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=2)
    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    await runtime.close()

    fresh, fresh_browser, fresh_blender, outcomes = await _restart_with_both(config)
    try:
        assert outcomes == []
        still = await fresh.workflow.get_task(task.task_id)
        assert still.status is TaskStatus.WAITING_FOR_CONFIRMATION
        assert "recovery" not in still.result
        assert fresh_blender.calls == []
        assert fresh_browser.calls == 0
        assert (await fresh.workflow.get_run(task.run_id)).status.value == "running"
        assert "no interrupted tasks" in _recovery_component(await fresh.health()).detail
    finally:
        await fresh.close()


async def test_recovery_is_idempotent(config: RuntimeConfig) -> None:
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=1)
    selected = task.result["candidates"]
    blender.crash_after_write = True
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    await runtime.close()

    fresh, fresh_blender, outcomes = await _restarted(config)
    try:
        assert [outcome.action for outcome in outcomes] == ["verified_output"]
        after_first = await fresh.workflow.get_task(task.task_id)
        assert await fresh.recover() == []
        after_second = await fresh.workflow.get_task(task.task_id)
        assert after_second.status is after_first.status is TaskStatus.SUCCEEDED
        assert after_second.result == after_first.result
        assert len(fresh_blender.calls) == 0
        assert (await fresh.workflow.get_run(task.run_id)).status.value == "succeeded"
    finally:
        await fresh.close()


async def test_terminal_tasks_are_not_touched(config: RuntimeConfig) -> None:
    runtime, browser, _blender = await _first_runtime(config)
    succeeded = await _collect(runtime, browser, limit=1)
    await runtime.references.confirm(
        succeeded.task_id, ConfirmRequest(artifact_ids=succeeded.result["candidates"])
    )
    cancelled = await _collect(runtime, browser, limit=1)
    await runtime.references.cancel(cancelled.task_id)
    await runtime.close()

    fresh, _, outcomes = await _restarted(config)
    try:
        assert outcomes == []
        assert (await fresh.workflow.get_task(succeeded.task_id)).status is TaskStatus.SUCCEEDED
        assert (await fresh.workflow.get_task(cancelled.task_id)).status is TaskStatus.CANCELLED
    finally:
        await fresh.close()


# -- observability --------------------------------------------------------
async def test_health_reports_what_recovery_did(config: RuntimeConfig) -> None:
    runtime, browser, blender = await _first_runtime(config)
    task = await _collect(runtime, browser, limit=1)
    selected = task.result["candidates"]
    blender.crash_after_write = True
    with pytest.raises(SimulatedCrash):
        await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    await runtime.close()

    fresh, _, _outcomes = await _restarted(config)
    try:
        component = _recovery_component(await fresh.health())
        assert component.state is HealthState.OK
        assert "1 task(s) reconciled" in component.detail
        assert f"{task.task_id}=verified_output" in component.detail
        assert [outcome.task_id for outcome in fresh.recovery_report] == [task.task_id]
    finally:
        await fresh.close()


async def test_recovery_requires_a_started_runtime(config: RuntimeConfig) -> None:
    from deskweave_contracts import LifecycleError

    runtime = await Runtime.create(config)
    try:
        with pytest.raises(LifecycleError):
            await runtime.recover()
    finally:
        await runtime.close()


# -- helpers --------------------------------------------------------------
async def _restart_with_both(
    config: RuntimeConfig,
) -> tuple[Runtime, FakeBrowser, FakeBlender, list[Any]]:
    """A fresh runtime that keeps references to both adapters."""
    runtime = await Runtime.create(config)
    await runtime.start()
    runtime.artifacts._downloader = FakeDownloader(config.data_dir / "artifacts")  # type: ignore[attr-defined]
    browser = FakeBrowser()
    blender = FakeBlender(config.data_dir / "artifacts")
    await runtime.register_adapter(browser)
    await runtime.register_adapter(blender)
    outcomes = await runtime.recover()
    return runtime, browser, blender, outcomes


def _recovery_component(health: Any) -> Any:
    for component in health.components:
        if component.name == "recovery":
            return component
    raise AssertionError(f"no recovery component in {[c.name for c in health.components]}")


__all__ = ["WORKFLOW"]
