"""Workflow and task contracts.

A *workflow run* is one execution of a workflow definition. A *task* is a unit of
work inside a run. DeskWeave does **not** implement a general DAG engine in
Phase 0; these models plus the runtime ``workflow`` module define the state
machine and persistence seams that a future engine will use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorInfo


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    """State of a single task."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class WorkflowRunStatus(StrEnum):
    """State of a workflow run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            WorkflowRunStatus.SUCCEEDED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }


class TaskRecord(BaseModel):
    """Persisted task row."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    name: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    error: ErrorInfo | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunRecord(BaseModel):
    """Persisted workflow-run row."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ExecutionRecord(BaseModel):
    """Audit record for a single adapter execution."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    adapter_id: str
    capability: str
    task_id: str | None = None
    ok: bool = False
    error: ErrorInfo | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None


__all__ = [
    "ExecutionRecord",
    "TaskRecord",
    "TaskStatus",
    "WorkflowRunRecord",
    "WorkflowRunStatus",
]
