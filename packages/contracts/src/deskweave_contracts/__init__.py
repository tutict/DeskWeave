"""DeskWeave shared contracts.

Dependency-light, framework-agnostic data models and the unified error hierarchy
used by every other DeskWeave component. This package must not import any other
DeskWeave package.
"""

from __future__ import annotations

from .adapters import (
    AdapterDescriptor,
    AdapterStatus,
    Capability,
    OperationRequest,
    OperationResult,
)
from .artifacts import ArtifactKind, ArtifactMetadata, ArtifactRef
from .errors import (
    AdapterConnectionError,
    AdapterError,
    AdapterExecutionError,
    AdapterTimeoutError,
    ConfigError,
    ConflictError,
    DeskWeaveError,
    ErrorInfo,
    LifecycleError,
    NotFoundError,
    NotImplementedYetError,
    PermissionDeniedError,
    PersistenceError,
    ValidationError,
)
from .events import Event, EventType
from .ids import (
    ArtifactId,
    EventId,
    ExecutionId,
    TaskId,
    WorkflowRunId,
    new_artifact_id,
    new_event_id,
    new_execution_id,
    new_id,
    new_task_id,
    new_workflow_run_id,
)
from .permissions import (
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    PermissionScope,
    RiskLevel,
)
from .workflow import (
    ExecutionRecord,
    TaskRecord,
    TaskStatus,
    WorkflowRunRecord,
    WorkflowRunStatus,
)

__version__ = "0.0.1"

__all__ = [
    "AdapterConnectionError",
    "AdapterDescriptor",
    "AdapterError",
    "AdapterExecutionError",
    "AdapterStatus",
    "AdapterTimeoutError",
    "ArtifactId",
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactRef",
    "Capability",
    "ConfigError",
    "ConflictError",
    "DeskWeaveError",
    "ErrorInfo",
    "Event",
    "EventId",
    "EventType",
    "ExecutionId",
    "ExecutionRecord",
    "LifecycleError",
    "NotFoundError",
    "NotImplementedYetError",
    "OperationRequest",
    "OperationResult",
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionRequest",
    "PermissionResult",
    "PermissionScope",
    "PersistenceError",
    "RiskLevel",
    "TaskId",
    "TaskRecord",
    "TaskStatus",
    "ValidationError",
    "WorkflowRunId",
    "WorkflowRunRecord",
    "WorkflowRunStatus",
    "__version__",
    "new_artifact_id",
    "new_event_id",
    "new_execution_id",
    "new_id",
    "new_task_id",
    "new_workflow_run_id",
]
