"""Unit tests for the reference workflow (collect → confirm → act).

The browser and Blender connectors are replaced by in-memory fakes that keep the
*same* trust boundary: the fake target still goes through ``Adapter.execute`` and
therefore through the real permission layer. Only network I/O and Blender itself
are simulated, so the assertions are about orchestration and authorisation, not
about Playwright or bpy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deskweave_contracts import (
    ArtifactKind,
    CollectionRequest,
    ConfirmRequest,
    ConflictError,
    OperationRequest,
    PermissionDeniedError,
    TaskStatus,
    ValidationError,
)
from deskweave_runtime import Runtime
from deskweave_runtime.references import public_artifact
from support.doubles import (
    COLLECT,
    CREATE_PLANE,
    FakeBlender,
    FakeBrowser,
    FakeDownloader,
    candidates,
)


@pytest.fixture
def wiring(runtime: Runtime) -> tuple[FakeBrowser, FakeBlender, FakeDownloader]:
    downloader = FakeDownloader(runtime.config.data_dir / "artifacts")
    # Swap only the transport; the store's policy code stays under test.
    runtime.artifacts._downloader = downloader  # type: ignore[attr-defined]
    blender = FakeBlender(runtime.config.data_dir / "artifacts")
    return FakeBrowser(), blender, downloader


async def _collect(
    runtime: Runtime,
    browser: FakeBrowser,
    blender: FakeBlender,
    *,
    limit: int = 2,
    query: str = "anime character reference",
) -> Any:
    await runtime.register_adapter(browser)
    await runtime.register_adapter(blender)
    task = await runtime.references.start_collection(CollectionRequest(query=query, limit=limit))
    return await runtime.references.wait_until_settled(task.task_id)


# -- collection -----------------------------------------------------------
async def test_collection_reaches_waiting_for_confirmation(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, downloader = wiring
    browser.candidates = candidates(2)

    task = await _collect(runtime, browser, blender)

    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    assert task.error is None
    assert task.result["query"] == "anime character reference"
    assert task.result["source"] == "bing"
    assert len(task.result["candidates"]) == 2
    assert task.result["failures"] == []
    assert downloader.requested == [f"https://cdn.example.com/ref-{i}.png" for i in range(2)]

    view = await runtime.references.get_view(task.task_id)
    assert [artifact.kind for artifact in view.artifacts] == [
        ArtifactKind.IMAGE,
        ArtifactKind.IMAGE,
    ]
    for artifact in view.artifacts:
        assert artifact.task_id == task.task_id
        assert artifact.metadata.source == "bing"
        assert artifact.metadata.title.startswith("Reference")
        assert artifact.metadata.original_url.startswith("https://cdn.example.com/ref-")
        assert artifact.metadata.source_page.startswith("https://example.com/page-")
        assert artifact.metadata.sha256 is not None
        assert (artifact.metadata.width, artifact.metadata.height) == (6, 4)
        # The file really exists, in the runtime's artifact directory.
        path = await runtime.artifacts.resolve_path(artifact.artifact_id)
        assert path.is_file()
        assert path.parent == (runtime.config.data_dir / "artifacts").resolve()

    # The workflow run is still open — it is waiting for a human.
    run = await runtime.workflow.get_run(task.run_id)
    assert run.status.value == "running"


async def test_partial_download_failures_are_reported_not_hidden(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(2, broken=True)

    task = await _collect(runtime, browser, blender)

    assert task.status is TaskStatus.WAITING_FOR_CONFIRMATION
    assert len(task.result["candidates"]) == 2
    assert len(task.result["failures"]) == 1
    assert task.result["failures"][0]["url"].endswith("broken.png")
    assert "simulated fetch failure" in task.result["failures"][0]["reason"]


async def test_no_downloadable_candidate_fails_the_task(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = [
        {
            "image_url": "https://cdn.example.com/broken.png",
            "page_url": "https://example.com/page-broken",
            "title": "Broken",
        }
    ]

    task = await _collect(runtime, browser, blender)

    assert task.status is TaskStatus.FAILED
    assert task.error is not None
    assert task.error.code == "no_candidates"
    assert task.result.get("candidates") == [] or task.result.get("candidates") is None
    run = await runtime.workflow.get_run(task.run_id)
    assert run.status.value == "failed"


async def test_empty_search_result_fails_the_task(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = []

    task = await _collect(runtime, browser, blender)

    assert task.status is TaskStatus.FAILED
    assert task.error is not None and task.error.code == "no_candidates"


async def test_collection_timeout_is_reported(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader], monkeypatch
) -> None:
    from deskweave_runtime.references import service as service_module

    monkeypatch.setattr(service_module, "COLLECTION_TIMEOUT_SECONDS", 0.05)
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    browser.delay = 1.0

    task = await _collect(runtime, browser, blender)

    assert task.status is TaskStatus.FAILED
    assert task.error is not None
    assert task.error.code in {"adapter_timeout_error", "collection_failed"}


# -- confirmation boundary ------------------------------------------------
async def test_target_is_denied_before_confirmation(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    """Calling the target directly must fail: no user confirmation happened."""
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    artifact_id = task.result["candidates"][0]

    result = await runtime.registry.get("blender").execute(
        OperationRequest(capability=CREATE_PLANE, arguments={"artifact_ids": [artifact_id]})
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert "confirmation" in result.error.message
    assert blender.calls == []
    # The task is untouched by the denied attempt.
    assert (await runtime.workflow.get_task(task.task_id)).status is (
        TaskStatus.WAITING_FOR_CONFIRMATION
    )


async def test_confirm_executes_target_and_is_idempotent(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(2)
    task = await _collect(runtime, browser, blender)
    selected = task.result["candidates"][:2]

    first = await runtime.references.confirm(
        task.task_id, ConfirmRequest(artifact_ids=selected, plane_name="AnimeRef")
    )

    assert first.status is TaskStatus.SUCCEEDED
    assert first.error is None
    assert first.result["confirmation"]["artifact_ids"] == selected
    assert first.result["confirmation"]["confirmed_at"]
    assert first.result["confirmation"]["plane_name"] == "AnimeRef"
    assert first.result["execution"]["ok"] is True
    assert first.result["execution"]["plane"]["image_count"] == 2
    assert blender.calls == [selected]
    assert len(blender.resolved) == 2
    assert all(path.is_file() for path in blender.resolved)
    # The produced .blend is registered as a model artifact for the same task.
    blend_id = first.result["execution"]["blend_artifact_id"]
    blend_artifact = await runtime.artifacts.get(blend_id)
    assert blend_artifact.kind is ArtifactKind.MODEL
    assert blend_artifact.task_id == task.task_id

    second = await runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected))
    assert second.status is TaskStatus.SUCCEEDED
    assert len(blender.calls) == 1, "a repeat confirmation must not re-execute the target"

    run = await runtime.workflow.get_run(task.run_id)
    assert run.status.value == "succeeded"


async def test_confirm_rejects_artifacts_from_another_task(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(2)
    task_a = await _collect(runtime, browser, blender)
    own = task_a.result["candidates"][0]

    foreign = await runtime.artifacts.register(
        kind=ArtifactKind.IMAGE, media_type="image/png", task_id="task_someone_else"
    )

    with pytest.raises(PermissionDeniedError):
        await runtime.references.confirm(
            task_a.task_id, ConfirmRequest(artifact_ids=[own, foreign.artifact_id])
        )

    assert blender.calls == []
    assert (await runtime.workflow.get_task(task_a.task_id)).status is (
        TaskStatus.WAITING_FOR_CONFIRMATION
    )


async def test_confirm_rejects_unknown_artifact_id(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)

    with pytest.raises(PermissionDeniedError):
        await runtime.references.confirm(
            task.task_id, ConfirmRequest(artifact_ids=["art_does_not_exist"])
        )


async def test_confirm_rejects_unsupported_target(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    selected = task.result["candidates"]

    with pytest.raises(ValidationError) as excinfo:
        await runtime.references.confirm(
            task.task_id, ConfirmRequest(artifact_ids=selected, target="filesystem")
        )
    assert excinfo.value.details["allowed"] == ["blender"]
    assert blender.calls == []
    assert (await runtime.workflow.get_task(task.task_id)).status is (
        TaskStatus.WAITING_FOR_CONFIRMATION
    )


async def test_confirm_on_cancelled_task_is_rejected(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)

    cancelled = await runtime.references.cancel(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED

    with pytest.raises(ConflictError):
        await runtime.references.confirm(
            task.task_id, ConfirmRequest(artifact_ids=task.result["candidates"])
        )
    assert blender.calls == []


async def test_cancel_after_success_is_rejected(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    await runtime.references.confirm(
        task.task_id, ConfirmRequest(artifact_ids=task.result["candidates"])
    )

    with pytest.raises(ConflictError):
        await runtime.references.cancel(task.task_id)


async def test_cancel_during_collection_stops_the_task(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    browser.delay = 0.5

    await runtime.register_adapter(browser)
    await runtime.register_adapter(blender)
    task = await runtime.references.start_collection(CollectionRequest(query="slow query"))
    await asyncio.sleep(0.05)

    cancelled = await runtime.references.cancel(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    await asyncio.sleep(0.05)
    assert (await runtime.workflow.get_task(task.task_id)).status is TaskStatus.CANCELLED


async def test_target_failure_marks_task_failed(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)

    blender.fail = True
    result = await runtime.references.confirm(
        task.task_id, ConfirmRequest(artifact_ids=task.result["candidates"])
    )

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == "target_execution_failed"
    assert result.result["execution"]["ok"] is False
    assert (await runtime.workflow.get_run(task.run_id)).status.value == "failed"


async def test_concurrent_confirms_execute_only_once(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    selected = task.result["candidates"]

    outcomes = await asyncio.gather(
        runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected)),
        runtime.references.confirm(task.task_id, ConfirmRequest(artifact_ids=selected)),
        return_exceptions=True,
    )

    assert len(blender.calls) == 1
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    conflicts = [item for item in outcomes if isinstance(item, ConflictError)]
    assert len(successes) + len(conflicts) == 2
    assert successes, "one confirmation must win"
    assert successes[0].status is TaskStatus.SUCCEEDED


async def test_confirmed_flag_in_arguments_grants_nothing(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    """An agent-supplied ``confirmed`` argument must not authorise the target."""
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    artifact_id = task.result["candidates"][0]

    result = await runtime.registry.get("blender").execute(
        OperationRequest(
            capability=CREATE_PLANE,
            arguments={"artifact_ids": [artifact_id], "confirmed": True},
        )
    )
    # ``additionalProperties: false`` rejects it outright.
    assert result.ok is False
    assert result.error is not None and result.error.code == "validation_error"
    assert blender.calls == []


# -- read model -----------------------------------------------------------
async def test_public_artifact_hides_paths(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    artifact = (await runtime.references.get_view(task.task_id)).artifacts[0]

    payload = public_artifact(artifact)

    assert payload["artifact_id"] == artifact.artifact_id
    assert "uri" not in payload
    assert "extra" not in payload["metadata"]
    assert payload["metadata"]["source_page"].startswith("https://example.com/")
    assert str(runtime.config.data_dir) not in str(payload)


async def test_list_views_filters_by_status(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)

    waiting = await runtime.references.list_views(status=TaskStatus.WAITING_FOR_CONFIRMATION)
    failed = await runtime.references.list_views(status=TaskStatus.FAILED)

    assert [view.task.task_id for view in waiting] == [task.task_id]
    assert failed == []


async def test_executions_are_recorded_for_audit(
    runtime: Runtime, wiring: tuple[FakeBrowser, FakeBlender, FakeDownloader]
) -> None:
    browser, blender, _ = wiring
    browser.candidates = candidates(1)
    task = await _collect(runtime, browser, blender)
    await runtime.references.confirm(
        task.task_id, ConfirmRequest(artifact_ids=task.result["candidates"])
    )

    records = await runtime.executions.list()
    by_capability = {record.capability: record for record in records}
    assert COLLECT in by_capability
    assert CREATE_PLANE in by_capability
    assert by_capability[COLLECT].task_id == task.task_id
    assert by_capability[CREATE_PLANE].task_id == task.task_id
    assert by_capability[CREATE_PLANE].ok is True
