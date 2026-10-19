"""Unit tests for the workflow / task state machine."""

from __future__ import annotations

import pytest

from deskweave_contracts import (
    EventType,
    LifecycleError,
    NotFoundError,
    TaskStatus,
    WorkflowRunStatus,
)
from deskweave_runtime import Runtime


async def test_run_and_task_lifecycle(runtime: Runtime) -> None:
    run = await runtime.workflow.start_run("demo")
    assert run.status is WorkflowRunStatus.RUNNING

    task = await runtime.workflow.create_task(run.run_id, "collect")
    assert task.status is TaskStatus.PENDING

    task = await runtime.workflow.set_task_status(task.task_id, TaskStatus.RUNNING)
    task = await runtime.workflow.set_task_status(task.task_id, TaskStatus.WAITING_FOR_CONFIRMATION)
    task = await runtime.workflow.set_task_status(task.task_id, TaskStatus.SUCCEEDED)
    assert task.status is TaskStatus.SUCCEEDED

    run = await runtime.workflow.finish_run(run.run_id, WorkflowRunStatus.SUCCEEDED)
    assert run.status is WorkflowRunStatus.SUCCEEDED


async def test_illegal_task_transition_raises(runtime: Runtime) -> None:
    run = await runtime.workflow.start_run("demo")
    task = await runtime.workflow.create_task(run.run_id, "t")
    with pytest.raises(LifecycleError):
        await runtime.workflow.set_task_status(task.task_id, TaskStatus.SUCCEEDED)


async def test_terminal_task_cannot_transition(runtime: Runtime) -> None:
    run = await runtime.workflow.start_run("demo")
    task = await runtime.workflow.create_task(run.run_id, "t")
    await runtime.workflow.cancel_task(task.task_id)
    with pytest.raises(LifecycleError):
        await runtime.workflow.set_task_status(task.task_id, TaskStatus.RUNNING)


async def test_create_task_for_unknown_run_raises(runtime: Runtime) -> None:
    with pytest.raises(NotFoundError):
        await runtime.workflow.create_task("missing-run", "t")


async def test_status_changes_emit_events(runtime: Runtime) -> None:
    seen: list[EventType] = []
    runtime.events.subscribe(lambda event: seen.append(event.type))
    run = await runtime.workflow.start_run("demo")
    await runtime.workflow.create_task(run.run_id, "t")
    assert EventType.WORKFLOW_RUN_STATUS_CHANGED in seen
    assert EventType.TASK_STATUS_CHANGED in seen
