"""Restart recovery for tasks left non-terminal by a previous process.

A task row is the only durable record of what the runtime was doing when it
stopped, so recovery reads the database and asks one question per task: *is the
recorded state consistent with the evidence that exists?*  Two rules hold for
every branch:

* nothing that needs a user confirmation is ever executed automatically;
* a task only becomes terminal when the evidence says it is.

Policies
--------
``pending``
    Created but never started (a crash inside ``start_collection``). Nothing was
    collected, so it is failed with the ``interrupted`` code.
``running`` without a recorded confirmation
    An in-flight collection. Downloaded images are already durable artifacts, so
    they are kept: with at least one candidate the task returns to
    ``waiting_for_confirmation`` and the user decides; with none it fails.
``running`` with a recorded confirmation
    An in-flight *target execution*. This is the dangerous case, because the
    target may already have run. The adapter is asked for the file it would have
    written (:class:`~deskweave_adapter_sdk.RecoverableOutput`); if the file
    exists **and re-verifies**, the task succeeds with that artifact. Otherwise
    the task returns to ``waiting_for_confirmation``: the user must confirm again
    explicitly, which is what authorises any retry.
``waiting_for_confirmation``
    A legitimate resting state. Recovery does not touch it — doing so would be
    exactly the "auto-execute an unconfirmed task" that is forbidden.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deskweave_adapter_sdk import AdapterRegistry, RecoverableOutput
from deskweave_contracts import (
    ArtifactKind,
    DeskWeaveError,
    ErrorInfo,
    LifecycleError,
    NotFoundError,
    TaskRecord,
    TaskStatus,
    WorkflowRunStatus,
)

from ..artifacts import ArtifactStore
from ..workflow import WorkflowEngine

#: How many interrupted tasks one start reconciles. A pathological database must
#: not turn startup into an unbounded operation; the rest are handled next start.
RECOVERY_BATCH_SIZE = 20

#: Timeout for "where would the output be?" — a local stat, so this is generous.
LOOKUP_TIMEOUT_SECONDS = 15.0

#: Timeout for re-verifying a recovered file. Verifying opens the file in the
#: target application, which is slower than a stat but still bounded.
VERIFY_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class RecoveryOutcome:
    """What recovery did with one task."""

    task_id: str
    action: str
    status: TaskStatus
    detail: str = ""


async def register_blend_output(
    artifacts: ArtifactStore, task_id: str, path: Path, logger: logging.Logger
) -> tuple[str | None, str | None]:
    """Register a ``.blend`` produced by the target as a model artifact.

    Returns ``(artifact_id, error_message)``. A file that exists but cannot be
    made addressable is reported, never silently dropped: the scene was produced,
    the task must not pretend otherwise.
    """
    try:
        artifact = await artifacts.register(
            kind=ArtifactKind.MODEL,
            uri=path.resolve().as_uri(),
            media_type="application/x-blender",
            size_bytes=path.stat().st_size,
            task_id=task_id,
        )
    except DeskWeaveError as exc:
        logger.warning("could not register blend output for task %s: %s", task_id, exc.message)
        return None, exc.message
    except OSError as exc:
        logger.warning("could not stat blend output for task %s: %s", task_id, exc)
        return None, str(exc)
    return artifact.artifact_id, None


async def finish_run_for_task(
    workflow: WorkflowEngine, task: TaskRecord, status: TaskStatus, logger: logging.Logger
) -> None:
    """Close the workflow run that owns ``task`` when ``status`` is terminal."""
    run_status = {
        TaskStatus.SUCCEEDED: WorkflowRunStatus.SUCCEEDED,
        TaskStatus.FAILED: WorkflowRunStatus.FAILED,
        TaskStatus.CANCELLED: WorkflowRunStatus.CANCELLED,
    }.get(status)
    if run_status is None:
        return
    try:
        run = await workflow.get_run(task.run_id)
        if run.status.is_terminal:
            return
        await workflow.finish_run(task.run_id, run_status)
    except (NotFoundError, LifecycleError) as exc:
        logger.warning("could not finish run %s: %s", task.run_id, exc)


class TaskRecovery:
    """Reconciles tasks that a previous process left non-terminal."""

    def __init__(
        self,
        *,
        workflow: WorkflowEngine,
        artifacts: ArtifactStore,
        registry: AdapterRegistry,
        target_adapter_id: str = "blender",
        batch_size: int = RECOVERY_BATCH_SIZE,
        verify_timeout: float = VERIFY_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._workflow = workflow
        self._artifacts = artifacts
        self._registry = registry
        self._target_adapter_id = target_adapter_id
        self._batch_size = batch_size
        self._verify_timeout = verify_timeout
        self._logger = logger or logging.getLogger("deskweave.recovery")

    # -- entry point ------------------------------------------------------
    async def run(self) -> list[RecoveryOutcome]:
        """Reconcile interrupted tasks. Never raises for a single bad task."""
        try:
            tasks = await self._workflow.list_tasks(
                statuses=(TaskStatus.PENDING, TaskStatus.RUNNING), limit=self._batch_size
            )
        except Exception:
            # A broken database is a startup problem, not a recovery problem:
            # report it and let the caller decide whether to continue.
            self._logger.exception("could not list interrupted tasks")
            return []

        if len(tasks) == self._batch_size:
            self._logger.warning(
                "recovery batch of %d task(s) is full; any remaining interrupted tasks "
                "will be reconciled on the next start",
                self._batch_size,
            )

        outcomes: list[RecoveryOutcome] = []
        for task in tasks:
            try:
                outcome = await self._recover(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.exception("recovery failed for task %s", task.task_id)
                outcomes.append(
                    RecoveryOutcome(
                        task_id=task.task_id,
                        action="error",
                        status=task.status,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if outcome is None:
                continue
            self._logger.warning(
                "task %s was left %s by the previous run; recovery: %s -> %s (%s)",
                task.task_id,
                task.status.value,
                outcome.action,
                outcome.status.value,
                outcome.detail or "no detail",
            )
            outcomes.append(outcome)
        return outcomes

    # -- per-task policies ------------------------------------------------
    async def _recover(self, task: TaskRecord) -> RecoveryOutcome | None:
        if task.status is TaskStatus.PENDING:
            return await self._fail(
                task,
                code="interrupted",
                action="never_started",
                message="the runtime stopped before this task started; start it again",
            )
        if task.status is not TaskStatus.RUNNING:
            return None

        confirmation = task.result.get("confirmation")
        if isinstance(confirmation, dict) and confirmation:
            return await self._recover_confirmed(task, confirmation)
        return await self._recover_collection(task)

    async def _recover_collection(self, task: TaskRecord) -> RecoveryOutcome:
        images = [
            artifact
            for artifact in await self._artifacts.list_for_task(task.task_id)
            if artifact.kind is ArtifactKind.IMAGE
        ]
        if not images:
            return await self._fail(
                task,
                code="interrupted",
                action="collection_interrupted",
                message=(
                    "the runtime stopped while collecting reference images and none were "
                    "saved; start a new collection"
                ),
            )

        candidate_ids = [artifact.artifact_id for artifact in images]
        result = {
            **task.result,
            "candidates": candidate_ids,
            "failures": list(task.result.get("failures") or []),
            "recovery": self._note(
                "resumed_partial_collection",
                f"{len(candidate_ids)} downloaded image(s) survived the interruption",
            ),
        }
        updated = await self._workflow.set_task_status(
            task.task_id, TaskStatus.WAITING_FOR_CONFIRMATION, result=result
        )
        return RecoveryOutcome(
            task_id=task.task_id,
            action="resumed_partial_collection",
            status=updated.status,
            detail=f"{len(candidate_ids)} candidate(s) kept",
        )

    async def _recover_confirmed(
        self, task: TaskRecord, confirmation: dict[str, Any]
    ) -> RecoveryOutcome:
        selected = [str(item) for item in (confirmation.get("artifact_ids") or [])]
        name = confirmation.get("plane_name")
        arguments: dict[str, Any] = {"artifact_ids": selected}
        if isinstance(name, str) and name:
            arguments["name"] = name

        recorded = task.result.get("execution")
        if isinstance(recorded, dict) and "ok" in recorded:
            # Defensive: status and result are written in one statement, so this
            # pair should not exist. If a future refactor splits them, the
            # recorded outcome is still better evidence than "unknown".
            status = TaskStatus.SUCCEEDED if recorded.get("ok") else TaskStatus.FAILED
            error = None
            if status is TaskStatus.FAILED:
                error = ErrorInfo(
                    code="target_execution_failed",
                    message=str(recorded.get("message") or "target execution failed"),
                    details={"task_id": task.task_id, "recovered": True},
                )
            result = {
                **task.result,
                "recovery": self._note("recorded_execution", "the outcome was already recorded"),
            }
            updated = await self._workflow.set_task_status(
                task.task_id, status, result=result, error=error
            )
            await finish_run_for_task(self._workflow, updated, status, self._logger)
            return RecoveryOutcome(
                task_id=task.task_id,
                action="recorded_execution",
                status=updated.status,
                detail="used the execution record",
            )

        adapter = self._target()
        if adapter is None:
            return await self._require_confirmation(
                task,
                reason=(
                    "the target connector is not available, so the interrupted run "
                    "cannot be verified"
                ),
            )

        evidence: Path | None = None
        lookup_error: str | None = None
        if isinstance(adapter, RecoverableOutput):
            try:
                evidence = await asyncio.wait_for(
                    adapter.find_output(task_id=task.task_id, arguments=arguments),
                    timeout=LOOKUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                lookup_error = f"{type(exc).__name__}: {exc}"
                self._logger.warning(
                    "could not look for existing output of task %s: %s", task.task_id, lookup_error
                )

        if evidence is not None:
            try:
                verification = await asyncio.wait_for(
                    adapter.verify_output(evidence, arguments=arguments),
                    timeout=self._verify_timeout,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._logger.warning(
                    "output of task %s did not verify after the restart: %s", task.task_id, reason
                )
                return await self._require_confirmation(
                    task, reason=f"the recovered output did not verify ({reason})"
                )
            return await self._succeed_from_evidence(
                task, evidence, verification, arguments, selected
            )

        reason = (
            "no output file was produced before the interruption"
            if lookup_error is None
            else f"the output could not be inspected ({lookup_error})"
        )
        return await self._require_confirmation(task, reason=reason)

    # -- terminal / resting states ----------------------------------------
    async def _succeed_from_evidence(
        self,
        task: TaskRecord,
        evidence: Path,
        verification: dict[str, Any],
        arguments: dict[str, Any],
        selected: list[str],
    ) -> RecoveryOutcome:
        artifact_id, artifact_error = await register_blend_output(
            self._artifacts, task.task_id, evidence, self._logger
        )
        execution: dict[str, Any] = {
            "ok": True,
            "recovered": True,
            "verification": verification,
            "plane": {
                "name": arguments.get("name") or "ReferencePlane",
                "image_count": len(selected),
            },
        }
        if artifact_id is not None:
            execution["blend_artifact_id"] = artifact_id
        if artifact_error is not None:
            execution["blend_artifact_error"] = artifact_error
        result = {
            **task.result,
            "execution": execution,
            "recovery": self._note(
                "verified_output",
                "the target had written its file before the interruption and it re-verified",
            ),
        }
        updated = await self._workflow.set_task_status(
            task.task_id, TaskStatus.SUCCEEDED, result=result
        )
        await finish_run_for_task(self._workflow, updated, TaskStatus.SUCCEEDED, self._logger)
        return RecoveryOutcome(
            task_id=task.task_id,
            action="verified_output",
            status=updated.status,
            detail="recovered and re-verified the produced file",
        )

    async def _require_confirmation(self, task: TaskRecord, *, reason: str) -> RecoveryOutcome:
        """Return the task to the confirmation gate without executing anything."""
        previous = task.result.get("execution")
        result = {
            **task.result,
            "recovery": self._note(
                "confirmation_required",
                f"{reason}; the operation was not repeated automatically",
            ),
        }
        if isinstance(previous, dict):
            # Keep the interrupted attempt for diagnosis, but never treat it as
            # an outcome: the next confirmation starts from a clean slate.
            result["interrupted_execution"] = previous
        updated = await self._workflow.set_task_status(
            task.task_id, TaskStatus.WAITING_FOR_CONFIRMATION, result=result
        )
        return RecoveryOutcome(
            task_id=task.task_id,
            action="confirmation_required",
            status=updated.status,
            detail=reason,
        )

    async def _fail(
        self, task: TaskRecord, *, code: str, action: str, message: str
    ) -> RecoveryOutcome:
        result = {**task.result, "recovery": self._note(action, message)}
        updated = await self._workflow.set_task_status(
            task.task_id,
            TaskStatus.FAILED,
            error=ErrorInfo(code=code, message=message, details={"task_id": task.task_id}),
            result=result,
        )
        await finish_run_for_task(self._workflow, updated, TaskStatus.FAILED, self._logger)
        return RecoveryOutcome(
            task_id=task.task_id, action=action, status=updated.status, detail=message
        )

    # -- helpers ----------------------------------------------------------
    def _target(self) -> Any | None:
        try:
            return self._registry.get(self._target_adapter_id)
        except DeskWeaveError:
            self._logger.warning(
                "adapter %s is not registered; interrupted executions cannot be verified",
                self._target_adapter_id,
            )
            return None

    @staticmethod
    def _note(action: str, detail: str) -> dict[str, Any]:
        return {"action": action, "detail": detail, "at": datetime.now(UTC).isoformat()}


__all__ = [
    "LOOKUP_TIMEOUT_SECONDS",
    "RECOVERY_BATCH_SIZE",
    "VERIFY_TIMEOUT_SECONDS",
    "RecoveryOutcome",
    "TaskRecovery",
    "finish_run_for_task",
    "register_blend_output",
]
