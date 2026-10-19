"""Event contracts.

Events describe *what happened* in the runtime so the desktop UI and future
subscribers can react. Phase 0 uses a simple in-process event bus; the payload
model is transport-agnostic so it can later be forwarded over the local control
API or a sidecar channel without changing producers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .ids import new_event_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventType(StrEnum):
    """Well-known event types emitted by the runtime."""

    RUNTIME_STARTED = "runtime.started"
    RUNTIME_STOPPED = "runtime.stopped"
    ADAPTER_REGISTERED = "adapter.registered"
    ADAPTER_UNREGISTERED = "adapter.unregistered"
    ADAPTER_STATUS_CHANGED = "adapter.status_changed"
    TASK_STATUS_CHANGED = "task.status_changed"
    WORKFLOW_RUN_STATUS_CHANGED = "workflow_run.status_changed"
    ARTIFACT_CREATED = "artifact.created"


class Event(BaseModel):
    """An immutable event envelope."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=new_event_id)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


__all__ = ["Event", "EventType"]
