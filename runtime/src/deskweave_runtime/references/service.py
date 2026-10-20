"""Reference-image workflow: collect → confirm → act.

This service owns the *orchestration* that neither the MCP gateway nor the
desktop shell should own:

1. create a workflow run and a task, and return immediately with the task id;
2. run the browser connector in the background, import every candidate into the
   artifact store (which enforces the download policy);
3. park the task in ``waiting_for_confirmation`` — the agent cannot push it
   further;
4. on an explicit user confirmation from the desktop UI, validate that the chosen
   artifacts belong to this task, record the audit trail, authorise **one** target
   execution through the permission layer, and run the target adapter;
5. persist the outcome and finish the run.

Nothing here is specific to anime modelling: the reference workflow is generic
"collect, choose, act" infrastructure. Domain plugins decide *which* adapters and
*which* prompts are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deskweave_adapter_sdk import AdapterRegistry
from deskweave_contracts import (
    AdapterConnectionError,
    ArtifactRef,
    CollectionRequest,
    ConfirmRequest,
    ConflictError,
    DeskWeaveError,
    ErrorInfo,
    LifecycleError,
    NotFoundError,
    OperationRequest,
    OperationResult,
    PermissionDeniedError,
    ReferenceSource,
    ReferenceTaskView,
    TaskRecord,
    TaskStatus,
    ValidationError,
)

from ..artifacts import ArtifactStore
from ..events import EventBus
from ..permissions import PermissionManager
from ..workflow import WorkflowEngine
from .recovery import RecoveryOutcome, TaskRecovery, finish_run_for_task, register_blend_output

WORKFLOW_NAME = "reference-collection"
TARGET_CAPABILITY = "create_reference_plane"
COLLECTION_CAPABILITY = "collect_reference_images"
COLLECTION_TIMEOUT_SECONDS = 120.0
TARGET_TIMEOUT_SECONDS = 180.0

#: Records an execution for the audit log. The runtime wires this to
#: ``Runtime._record_execution`` so that adapter calls made *by this service*
#: (not only those made through ``Runtime.execute``) appear in the audit trail.
ExecutionRecorder = Callable[[str, OperationRequest, OperationResult], Awaitable[None]]


class ReferenceService:
    """Runs the reference workflow and enforces its confirmation boundary."""

    def __init__(
        self,
        *,
        workflow: WorkflowEngine,
        artifacts: ArtifactStore,
        registry: AdapterRegistry,
        permissions: PermissionManager,
        events: EventBus,
        browser_adapter_id: str = "browser",
        target_adapter_id: str = "blender",
        execution_recorder: ExecutionRecorder | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._workflow = workflow
        self._artifacts = artifacts
        self._registry = registry
        self._permissions = permissions
        self._events = events
        self._browser_adapter_id = browser_adapter_id
        #: source name -> connector id. ``bing`` is the built-in default; every
        #: other source is wired by the composition root, so this module never
        #: learns the name of a concrete connector.
        self._collection_sources: dict[str, str] = {ReferenceSource.BING.value: browser_adapter_id}
        self._target_adapter_id = target_adapter_id
        self._recorder = execution_recorder
        self._logger = logger or logging.getLogger("deskweave.references")
        self._background: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._recovery = TaskRecovery(
            workflow=workflow,
            artifacts=artifacts,
            registry=registry,
            target_adapter_id=target_adapter_id,
            logger=self._logger,
        )

    # -- collection -------------------------------------------------------
    def register_source(self, source: str, adapter_id: str) -> None:
        """Route a reference source to the connector that serves it.

        Called by the composition root, which is the only layer that knows which
        connectors exist. Without an entry a source simply is not available; the
        workflow does not guess and never falls back to a different connector.
        """
        self._collection_sources[source] = adapter_id
        self._logger.debug("reference source %s routed to adapter %s", source, adapter_id)

    def adapter_id_for_source(self, source: str) -> str:
        """Return the connector id for ``source`` or explain why there is none."""
        adapter_id = self._collection_sources.get(source)
        if adapter_id is None:
            raise AdapterConnectionError(
                f"no connector is registered for reference source {source!r}",
                details={
                    "source": source,
                    "registered_sources": sorted(self._collection_sources),
                },
            )
        return adapter_id

    async def start_collection(self, request: CollectionRequest) -> TaskRecord:
        """Create a task and start collecting in the background.

        Returns as soon as the task exists; the caller (MCP or UI) polls
        :meth:`get_view` instead of blocking on a human.
        """
        # Fail fast: a source with no connector is a configuration error, and it
        # should surface to the caller rather than become a failed task.
        self._registry.get(self.adapter_id_for_source(request.source.value))

        run = await self._workflow.start_run(WORKFLOW_NAME)
        task = await self._workflow.create_task(
            run.run_id, f"Collect reference images: {request.query}"
        )
        # Persist the request before doing any work: a crash mid-collection must
        # still leave behind *what* was being collected.
        task = await self._workflow.set_task_status(
            task.task_id, TaskStatus.RUNNING, result=self._request_payload(request)
        )

        self._background[task.task_id] = asyncio.create_task(
            self._collect(task.task_id, request), name=f"collect-{task.task_id}"
        )
        self._logger.info("started reference collection task %s", task.task_id)
        return task

    async def _collect(self, task_id: str, request: CollectionRequest) -> None:
        try:
            adapter_id = self.adapter_id_for_source(request.source.value)
            adapter = self._registry.get(adapter_id)
            op_request = OperationRequest(
                capability=COLLECTION_CAPABILITY,
                arguments={
                    "query": request.query,
                    "limit": request.limit,
                    "source": request.source.value,
                },
                timeout_seconds=COLLECTION_TIMEOUT_SECONDS,
                reason="agent requested reference images",
                task_id=task_id,
            )
            result = await adapter.execute(op_request)
            await self._record(adapter_id, op_request, result)
            if not result.ok or result.error is not None:
                await self._fail(task_id, self._error_from(result.error, "image collection failed"))
                return

            candidates = result.data.get("candidates") or []
            artifacts: list[str] = []
            failures: list[dict[str, str]] = []
            for candidate in candidates:
                url = str(candidate.get("image_url") or "")
                if not url:
                    continue
                try:
                    artifact = await self._artifacts.import_image(
                        url,
                        task_id=task_id,
                        title=candidate.get("title"),
                        source=request.source.value,
                        source_page=candidate.get("page_url"),
                        extra=self._attribution(candidate),
                    )
                    artifacts.append(artifact.artifact_id)
                except DeskWeaveError as exc:
                    failures.append({"url": url, "reason": exc.message})
                    self._logger.info("candidate rejected (%s): %s", url, exc.message)

            payload = {
                **self._request_payload(request),
                "candidates": artifacts,
                "failures": failures,
            }
            if not artifacts:
                await self._fail(
                    task_id,
                    ErrorInfo(
                        code="no_candidates",
                        message="no candidate image could be downloaded",
                        details={"failures": failures[:10]},
                    ),
                )
                return

            await self._workflow.set_task_status(
                task_id, TaskStatus.WAITING_FOR_CONFIRMATION, result=payload
            )
            self._logger.info(
                "task %s awaiting confirmation with %d candidate(s)", task_id, len(artifacts)
            )
        except asyncio.CancelledError:
            self._logger.info("collection for task %s was cancelled", task_id)
            raise
        except Exception as exc:
            self._logger.exception("collection for task %s crashed", task_id)
            await self._fail(
                task_id,
                ErrorInfo(
                    code="collection_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    details={"task_id": task_id},
                ),
            )
        finally:
            self._background.pop(task_id, None)

    @staticmethod
    def _request_payload(request: CollectionRequest) -> dict[str, Any]:
        """The durable part of a collection request, written before any work."""
        return {
            "request": request.model_dump(mode="json"),
            "query": request.query,
            "source": request.source.value,
        }

    @staticmethod
    def _attribution(candidate: dict[str, Any]) -> dict[str, str]:
        """Artifact metadata a source is allowed to contribute.

        ``thumbnail_url`` predates this; ``license``/``artist`` are what a
        CC-licensed source must carry so the provenance survives into the
        artifact. Empty values are dropped rather than stored as ``"None"``.
        """
        extra = {"thumbnail_url": str(candidate.get("thumbnail_url") or "")}
        for key in ("license", "artist"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                extra[key] = value.strip()
        return extra

    # -- recovery ---------------------------------------------------------
    async def recover_interrupted(self) -> list[RecoveryOutcome]:
        """Reconcile tasks left non-terminal by a previous process.

        Safe to call only before the runtime serves requests (that is, at
        startup): it must never race a live collection.
        """
        return await self._recovery.run()

    # -- reads ------------------------------------------------------------
    async def get_task(self, task_id: str) -> TaskRecord:
        return await self._workflow.get_task(task_id)

    async def get_view(self, task_id: str) -> ReferenceTaskView:
        task = await self._workflow.get_task(task_id)
        artifacts = await self._artifacts.list_for_task(task_id)
        return ReferenceTaskView(task=task, artifacts=artifacts)

    async def list_views(
        self, *, status: TaskStatus | None = None, limit: int | None = 50
    ) -> list[ReferenceTaskView]:
        tasks = await self._workflow.list_tasks(limit=limit)
        views: list[ReferenceTaskView] = []
        for task in tasks:
            if status is not None and task.status is not status:
                continue
            artifacts = await self._artifacts.list_for_task(task.task_id)
            views.append(ReferenceTaskView(task=task, artifacts=artifacts))
        return views

    # -- confirmation -----------------------------------------------------
    async def confirm(self, task_id: str, request: ConfirmRequest) -> TaskRecord:
        """Execute the confirmed selection exactly once.

        Raises ``ConflictError`` when the task is not awaiting confirmation or when
        a confirmation is already running, ``PermissionDeniedError`` when an
        artifact does not belong to the task.
        """
        lock = self._lock(task_id)
        async with lock:
            task = await self._workflow.get_task(task_id)

            confirmation = task.result.get("confirmation")
            if task.status is TaskStatus.SUCCEEDED and confirmation:
                # Idempotent replay: the user (or the UI) double-clicked.
                return task
            if task.status is TaskStatus.RUNNING and confirmation:
                raise ConflictError(
                    "confirmation is already being executed for this task",
                    details={"task_id": task_id, "status": task.status.value},
                )
            if task.status is not TaskStatus.WAITING_FOR_CONFIRMATION:
                raise ConflictError(
                    f"task is not awaiting confirmation (status={task.status.value})",
                    details={"task_id": task_id, "status": task.status.value},
                )

            artifacts = await self._artifacts.list_for_task(task_id)
            available = {artifact.artifact_id: artifact for artifact in artifacts}
            selected_ids = list(dict.fromkeys(request.artifact_ids))
            foreign = [artifact_id for artifact_id in selected_ids if artifact_id not in available]
            if foreign:
                raise PermissionDeniedError(
                    "selected artifacts do not belong to this task",
                    details={"task_id": task_id, "artifact_ids": foreign},
                )

            if request.target != self._target_adapter_id:
                raise ValidationError(
                    f"unsupported confirmation target {request.target!r}",
                    details={"allowed": [self._target_adapter_id]},
                )
            target = self._registry.get(request.target)

            confirmation_payload = {
                "artifact_ids": selected_ids,
                "target": request.target,
                "plane_name": request.plane_name,
                "confirmed_at": datetime.now(UTC).isoformat(),
            }
            result = {**task.result, "confirmation": confirmation_payload}
            task = await self._workflow.set_task_status(task_id, TaskStatus.RUNNING, result=result)

            try:
                execution = await self._execute_target(task_id, target, selected_ids, request)
            except Exception as exc:
                # A confirmed task must always reach a terminal state; otherwise
                # the UI would show a spinner forever and a retry would be
                # rejected as "already running".
                self._logger.exception("target execution crashed for task %s", task_id)
                execution = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
            result["execution"] = execution
            final_status = TaskStatus.SUCCEEDED if execution["ok"] else TaskStatus.FAILED
            error = None
            if not execution["ok"]:
                error = ErrorInfo(
                    code="target_execution_failed",
                    message=execution.get("message") or "target execution failed",
                    details={"task_id": task_id, "target": request.target},
                )
            task = await self._workflow.set_task_status(
                task_id, final_status, result=result, error=error
            )
            await self._finish_run(task, final_status)
            return task

    async def _execute_target(
        self,
        task_id: str,
        adapter: Any,
        artifact_ids: list[str],
        request: ConfirmRequest,
    ) -> dict[str, Any]:
        # The user confirmed *this* operation: allow exactly one call through the
        # permission layer. The allowance is consumed by the check below.
        self._permissions.allow_once(request.target, TARGET_CAPABILITY)
        op_request = OperationRequest(
            capability=TARGET_CAPABILITY,
            arguments={
                "artifact_ids": artifact_ids,
                **({"name": request.plane_name} if request.plane_name else {}),
            },
            timeout_seconds=TARGET_TIMEOUT_SECONDS,
            reason="user confirmed the reference selection",
            task_id=task_id,
        )
        try:
            result = await adapter.execute(op_request)
        except Exception as exc:  # defensive: execute() should not raise
            self._logger.exception("target execution crashed for task %s", task_id)
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        await self._record(request.target, op_request, result)

        if not result.ok or result.error is not None:
            error = result.error
            return {
                "ok": False,
                "message": error.message if error else "target execution failed",
                "code": error.code if error else "target_execution_failed",
            }

        data = dict(result.data)
        blend_path = data.pop("blend_file", None)
        if isinstance(blend_path, str):
            path = Path(blend_path)
            if path.is_file():
                artifact_id, artifact_error = await register_blend_output(
                    self._artifacts, task_id, path, self._logger
                )
                if artifact_id is not None:
                    data["blend_artifact_id"] = artifact_id
                if artifact_error is not None:
                    # The scene was produced, but its file is not addressable
                    # through the store. Report it instead of wedging the task.
                    data["blend_artifact_error"] = artifact_error
        data["ok"] = True
        return data

    # -- cancellation -----------------------------------------------------
    async def cancel(self, task_id: str) -> TaskRecord:
        lock = self._lock(task_id)
        async with lock:
            task = await self._workflow.get_task(task_id)
            if task.status is TaskStatus.CANCELLED:
                return task
            if task.status.is_terminal:
                raise ConflictError(
                    f"task is already {task.status.value} and cannot be cancelled",
                    details={"task_id": task_id, "status": task.status.value},
                )
            if task.result.get("confirmation") and task.status is TaskStatus.RUNNING:
                raise ConflictError(
                    "cannot cancel while the confirmed operation is running",
                    details={"task_id": task_id},
                )

            background = self._background.pop(task_id, None)
            if background is not None and not background.done():
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background

            task = await self._workflow.set_task_status(task_id, TaskStatus.CANCELLED)
            await self._finish_run(task, TaskStatus.CANCELLED)
            self._logger.info("task %s cancelled by user", task_id)
            return task

    # -- lifecycle --------------------------------------------------------
    async def shutdown(self) -> None:
        """Cancel in-flight collection tasks (called when the runtime closes)."""
        pending = list(self._background.items())
        self._background.clear()
        for _task_id, handle in pending:
            if not handle.done():
                handle.cancel()
        for task_id, handle in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handle
            self._logger.info("stopped background collection for %s", task_id)

    # -- helpers ----------------------------------------------------------
    async def _record(
        self, adapter_id: str, request: OperationRequest, result: OperationResult
    ) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder(adapter_id, request, result)
        except Exception:
            # An audit failure must never turn a successful operation into a
            # failure the user sees, but it must be visible in the logs.
            self._logger.exception("failed to record execution for adapter %s", adapter_id)

    def _lock(self, task_id: str) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    async def _fail(self, task_id: str, error: ErrorInfo) -> None:
        try:
            task = await self._workflow.get_task(task_id)
            if task.status.is_terminal:
                return
            task = await self._workflow.set_task_status(
                task_id, TaskStatus.FAILED, error=error, result=task.result
            )
            await self._finish_run(task, TaskStatus.FAILED)
        except LifecycleError:
            self._logger.warning("could not mark task %s failed: illegal transition", task_id)
        except NotFoundError:
            self._logger.warning("could not mark missing task %s failed", task_id)

    async def _finish_run(self, task: TaskRecord, status: TaskStatus) -> None:
        await finish_run_for_task(self._workflow, task, status, self._logger)

    @staticmethod
    def _error_from(error: ErrorInfo | None, default: str) -> ErrorInfo:
        if error is not None:
            return error
        return ErrorInfo(code="collection_failed", message=default)

    async def wait_until_settled(self, task_id: str, *, timeout: float = 30.0) -> TaskRecord:
        """Block until the background collection stops. Intended for tests."""
        handle = self._background.get(task_id)
        if handle is not None:
            await asyncio.wait_for(asyncio.shield(handle), timeout=timeout)
        return await self._workflow.get_task(task_id)


def public_artifact(artifact: ArtifactRef) -> dict[str, Any]:
    """Serialise an artifact without its local path.

    Agents and the webview get ids and descriptive metadata; ``uri`` stays inside
    the runtime because a path is never an authorisation token.
    """
    metadata = artifact.metadata.model_dump(exclude_none=True)
    metadata.pop("extra", None)
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind.value,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "metadata": metadata,
        "created_at": artifact.created_at.isoformat(),
    }


__all__ = [
    "COLLECTION_CAPABILITY",
    "COLLECTION_TIMEOUT_SECONDS",
    "TARGET_CAPABILITY",
    "WORKFLOW_NAME",
    "ExecutionRecorder",
    "ReferenceService",
    "public_artifact",
]
