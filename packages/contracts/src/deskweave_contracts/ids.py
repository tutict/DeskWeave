"""Opaque identifier helpers.

DeskWeave distinguishes *logical resource ids* (stable, opaque strings stored in
the database and referenced by agents) from *local file paths* (environment
specific, never used as an authorisation token). These helpers generate the
logical ids.
"""

from __future__ import annotations

import uuid
from typing import NewType

TaskId = NewType("TaskId", str)
WorkflowRunId = NewType("WorkflowRunId", str)
ArtifactId = NewType("ArtifactId", str)
ExecutionId = NewType("ExecutionId", str)
EventId = NewType("EventId", str)


def new_id(prefix: str) -> str:
    """Return a new opaque id of the form ``<prefix>_<32 hex chars>``."""
    if not prefix or not prefix.isalnum():
        raise ValueError("id prefix must be a non-empty alphanumeric string")
    return f"{prefix}_{uuid.uuid4().hex}"


def new_task_id() -> TaskId:
    return TaskId(new_id("task"))


def new_workflow_run_id() -> WorkflowRunId:
    return WorkflowRunId(new_id("wfr"))


def new_artifact_id() -> ArtifactId:
    return ArtifactId(new_id("art"))


def new_execution_id() -> ExecutionId:
    return ExecutionId(new_id("exec"))


def new_event_id() -> EventId:
    return EventId(new_id("evt"))


__all__ = [
    "ArtifactId",
    "EventId",
    "ExecutionId",
    "TaskId",
    "WorkflowRunId",
    "new_artifact_id",
    "new_event_id",
    "new_execution_id",
    "new_id",
    "new_task_id",
    "new_workflow_run_id",
]
