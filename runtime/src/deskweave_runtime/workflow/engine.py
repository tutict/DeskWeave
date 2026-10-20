"""Minimal workflow / task state machine.

This is **not** a general DAG engine. It records workflow runs and tasks, enforces
a small set of legal status transitions, and emits events. It is the seam a
future engine will build on: cancellation, failure and recovery are expressed as
status transitions here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from deskweave_contracts import (
    ErrorInfo,
    Event,
    EventType,
    LifecycleError,
    TaskRecord,
    TaskStatus,
    WorkflowRunRecord,
    WorkflowRunStatus,
    new_task_id,
    new_workflow_run_id,
)

from ..events import EventBus
from ..persistence import TaskRepository, WorkflowRunRepository

_ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_FOR_CONFIRMATION,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_CONFIRMATION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_ALLOWED_RUN_TRANSITIONS: dict[WorkflowRunStatus, frozenset[WorkflowRunStatus]] = {
    WorkflowRunStatus.PENDING: frozenset(
        {WorkflowRunStatus.RUNNING, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED}
    ),
    WorkflowRunStatus.RUNNING: frozenset(
        {
            WorkflowRunStatus.SUCCEEDED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
    ),
    WorkflowRunStatus.SUCCEEDED: frozenset(),
    WorkflowRunStatus.FAILED: frozenset(),
    WorkflowRunStatus.CANCELLED: frozenset(),
}


class WorkflowEngine:
    def __init__(
        self,
        tasks: TaskRepository,
        runs: WorkflowRunRepository,
        events: EventBus,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._events = events
        self._logger = logger or logging.getLogger("deskweave.workflow")

    async def start_run(self, workflow: str) -> WorkflowRunRecord:
        run = WorkflowRunRecord(
            run_id=new_workflow_run_id(),
            workflow=workflow,
            status=WorkflowRunStatus.RUNNING,
        )
        await self._runs.create(run)
        await self._emit_run(run)
        return run

    async def create_task(self, run_id: str, name: str) -> TaskRecord:
        # Ensures the run exists (raises NotFoundError otherwise).
        await self._runs.get(run_id)
        task = TaskRecord(task_id=new_task_id(), run_id=run_id, name=name)
        await self._tasks.create(task)
        await self._emit_task(task)
        return task

    async def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: ErrorInfo | None = None,
        result: dict | None = None,
    ) -> TaskRecord:
        current = await self._tasks.get(task_id)
        self._check_task_transition(current.status, status)
        task = await self._tasks.update(task_id, status=status, error=error, result=result)
        await self._emit_task(task)
        return task

    async def cancel_task(self, task_id: str) -> TaskRecord:
        return await self.set_task_status(task_id, TaskStatus.CANCELLED)

    async def finish_run(self, run_id: str, status: WorkflowRunStatus) -> WorkflowRunRecord:
        current = await self._runs.get(run_id)
        self._check_run_transition(current.status, status)
        run = await self._runs.update_status(run_id, status)
        await self._emit_run(run)
        return run

    async def list_tasks(
        self,
        *,
        run_id: str | None = None,
        statuses: Sequence[TaskStatus] | None = None,
        limit: int | None = None,
    ) -> list[TaskRecord]:
        return await self._tasks.list(run_id=run_id, statuses=statuses, limit=limit)

    async def get_task(self, task_id: str) -> TaskRecord:
        """Return one task or raise ``NotFoundError``."""
        return await self._tasks.get(task_id)

    async def get_run(self, run_id: str) -> WorkflowRunRecord:
        return await self._runs.get(run_id)

    async def list_runs(self, *, limit: int | None = None) -> list[WorkflowRunRecord]:
        return await self._runs.list(limit=limit)

    # -- internals --------------------------------------------------------
    @staticmethod
    def _check_task_transition(current: TaskStatus, new: TaskStatus) -> None:
        if new not in _ALLOWED_TASK_TRANSITIONS[current]:
            raise LifecycleError(
                f"illegal task transition {current.value!r} -> {new.value!r}",
                details={"from": current.value, "to": new.value},
            )

    @staticmethod
    def _check_run_transition(current: WorkflowRunStatus, new: WorkflowRunStatus) -> None:
        if new not in _ALLOWED_RUN_TRANSITIONS[current]:
            raise LifecycleError(
                f"illegal workflow run transition {current.value!r} -> {new.value!r}",
                details={"from": current.value, "to": new.value},
            )

    async def _emit_task(self, task: TaskRecord) -> None:
        await self._events.publish(
            Event(
                type=EventType.TASK_STATUS_CHANGED,
                payload={
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "status": task.status.value,
                },
            )
        )

    async def _emit_run(self, run: WorkflowRunRecord) -> None:
        await self._events.publish(
            Event(
                type=EventType.WORKFLOW_RUN_STATUS_CHANGED,
                payload={"run_id": run.run_id, "status": run.status.value},
            )
        )


__all__ = ["WorkflowEngine"]
