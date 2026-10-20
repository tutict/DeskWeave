"""The DeskWeave core runtime.

The runtime wires together configuration, the SQLite database, the event bus, the
permission layer, the workflow engine, the artifact store and the adapter
registry. It is created explicitly and owns the lifetime of its async resources.

Importing this module has no side effects: no processes are started and no global
runtime instance is created.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from deskweave_adapter_sdk import Adapter, AdapterRegistry
from deskweave_contracts import (
    AdapterDescriptor,
    Event,
    EventType,
    ExecutionRecord,
    LifecycleError,
    OperationRequest,
    OperationResult,
    new_execution_id,
)

from .artifacts import ArtifactOpener, ArtifactStore, ImageDownloader, ensure_artifact_root
from .config import RuntimeConfig
from .core import AdapterHealth, ComponentHealth, HealthState, RuntimeHealth
from .events import EventBus
from .logging import get_logger
from .permissions import PermissionManager
from .persistence import (
    AdapterInstallationRepository,
    ArtifactRepository,
    Database,
    ExecutionRepository,
    PermissionGrantRepository,
    TaskRepository,
    WorkflowRunRepository,
)
from .references import RecoveryOutcome, ReferenceService
from .workflow import WorkflowEngine

__version__ = "0.0.1"


class RuntimeState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CLOSED = "closed"


class Runtime:
    """The local core runtime."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._state = RuntimeState.CREATED
        self._logger: logging.Logger = get_logger("runtime")
        self._started_at: datetime | None = None
        self._recovery: list[RecoveryOutcome] | None = None

        self._database = Database(config.resolved_database_path)
        self._events = EventBus()
        self._runs = WorkflowRunRepository(self._database)
        self._tasks = TaskRepository(self._database)
        self._artifacts_repo = ArtifactRepository(self._database)
        self._grants_repo = PermissionGrantRepository(self._database)
        self._executions_repo = ExecutionRepository(self._database)
        self._installations = AdapterInstallationRepository(self._database)

        self._permissions = PermissionManager(
            grant_repository=self._grants_repo, logger=get_logger("permissions")
        )
        self._artifact_root = ensure_artifact_root(config.data_dir)
        self._artifacts = ArtifactStore(
            self._artifacts_repo,
            self._events,
            allowed_roots=[*_default_allowed_roots(config), self._artifact_root],
            downloader=ImageDownloader(self._artifact_root),
            logger=get_logger("artifacts"),
        )
        self._opener = ArtifactOpener(self._artifacts, logger=get_logger("artifacts.open"))
        self._registry = AdapterRegistry(
            permission_checker=self._permissions,
            resource_resolver=self._artifacts.resolve_path,
            logger=get_logger("registry"),
        )
        self._workflow = WorkflowEngine(
            self._tasks, self._runs, self._events, logger=get_logger("workflow")
        )
        self._references = ReferenceService(
            workflow=self._workflow,
            artifacts=self._artifacts,
            registry=self._registry,
            permissions=self._permissions,
            events=self._events,
            execution_recorder=self._record_execution,
            logger=get_logger("references"),
        )

    # -- construction -----------------------------------------------------
    @classmethod
    async def create(cls, config: RuntimeConfig) -> Runtime:
        """Create a runtime and open its database (does not start it yet)."""
        runtime = cls(config)
        await runtime._database.initialize()
        runtime._logger.info(
            "runtime created (db=%s, schema=%s)",
            runtime._database.path,
            await runtime._database.schema_version(),
        )
        return runtime

    # -- lifecycle --------------------------------------------------------
    @property
    def state(self) -> RuntimeState:
        return self._state

    async def start(self) -> None:
        """Mark the runtime as running. Raises if already started/closed."""
        if self._state is RuntimeState.RUNNING:
            raise LifecycleError("runtime is already started")
        if self._state is RuntimeState.CLOSED:
            raise LifecycleError("runtime is closed and cannot be restarted")
        self._state = RuntimeState.RUNNING
        self._started_at = datetime.now(UTC)
        await self._events.publish(Event(type=EventType.RUNTIME_STARTED, payload={}))
        self._logger.info("runtime started")

    async def close(self) -> None:
        """Tear down the runtime. Idempotent."""
        if self._state is RuntimeState.CLOSED:
            return
        await self._references.shutdown()
        await self._disconnect_all()
        await self._events.publish(Event(type=EventType.RUNTIME_STOPPED, payload={}))
        await self._database.close()
        self._state = RuntimeState.CLOSED
        self._logger.info("runtime closed")

    async def recover(self) -> list[RecoveryOutcome]:
        """Reconcile tasks that a previous process left non-terminal.

        Must run **after** the application has registered its adapters (recovery
        asks the target adapter to verify what it may already have produced) and
        **before** the runtime starts serving requests, so it can never race a
        live collection or confirmation.

        Returns one outcome per reconciled task. Never raises for a single bad
        task: a database that cannot be reconciled must not keep the app closed.
        """
        if self._state is not RuntimeState.RUNNING:
            raise LifecycleError("runtime must be started before recovery runs")
        self._recovery = await self._references.recover_interrupted()
        if self._recovery:
            self._logger.warning(
                "recovered %d interrupted task(s) from the previous run",
                len(self._recovery),
            )
        else:
            self._logger.info("no interrupted tasks to recover")
        return self._recovery

    @property
    def recovery_report(self) -> list[RecoveryOutcome]:
        """The outcomes of the last :meth:`recover` call (empty before it runs)."""
        return list(self._recovery or [])

    async def _disconnect_all(self) -> None:
        for adapter in list(self._registry):
            try:
                await adapter.disconnect()
            except Exception:
                self._logger.exception("failed to disconnect adapter %s", adapter.adapter_id)

    # -- accessors --------------------------------------------------------
    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def database(self) -> Database:
        return self._database

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def permissions(self) -> PermissionManager:
        return self._permissions

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    @property
    def workflow(self) -> WorkflowEngine:
        return self._workflow

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    @property
    def opener(self) -> ArtifactOpener:
        """Hands a verified artifact file to the operating system."""
        return self._opener

    @property
    def references(self) -> ReferenceService:
        return self._references

    @property
    def executions(self) -> ExecutionRepository:
        return self._executions_repo

    @property
    def installations(self) -> AdapterInstallationRepository:
        return self._installations

    # -- adapters ---------------------------------------------------------
    async def register_adapter(self, adapter: Adapter, *, replace: bool = False) -> None:
        """Register an adapter and record its installation metadata."""
        self._registry.register(adapter, replace=replace)
        descriptor = adapter.descriptor
        await self._installations.upsert(
            descriptor.adapter_id,
            name=descriptor.name,
            version=descriptor.version,
        )
        await self._events.publish(
            Event(
                type=EventType.ADAPTER_REGISTERED,
                payload={"adapter_id": adapter.adapter_id},
            )
        )

    async def unregister_adapter(self, adapter_id: str) -> Adapter:
        adapter = self._registry.unregister(adapter_id)
        await self._events.publish(
            Event(type=EventType.ADAPTER_UNREGISTERED, payload={"adapter_id": adapter_id})
        )
        return adapter

    def register_collection_source(self, source: str, adapter_id: str) -> None:
        """Declare which connector serves a reference-collection source.

        Composition-root API, called next to ``register_adapter``. It exists so a
        new source can be added without teaching the workflow the name of any
        concrete connector: the runtime core knows that sources exist, the
        composition root knows which ones.
        """
        self._references.register_source(source, adapter_id)

    async def list_adapters(self) -> list[AdapterDescriptor]:
        return self._registry.list_descriptors()

    async def execute(self, adapter_id: str, request: OperationRequest) -> OperationResult:
        """Execute a capability and record an audit entry."""
        adapter = self._registry.get(adapter_id)
        result = await adapter.execute(request)
        await self._record_execution(adapter_id, request, result)
        return result

    async def _record_execution(
        self, adapter_id: str, request: OperationRequest, result: OperationResult
    ) -> None:
        try:
            await self._executions_repo.create(
                ExecutionRecord(
                    execution_id=new_execution_id(),
                    adapter_id=adapter_id,
                    capability=request.capability,
                    task_id=request.task_id,
                    ok=result.ok,
                    error=result.error,
                    created_at=result.started_at,
                    finished_at=result.finished_at,
                )
            )
        except Exception:
            self._logger.exception("failed to record execution for adapter %s", adapter_id)

    # -- health -----------------------------------------------------------
    async def health(self) -> RuntimeHealth:
        schema_version = 0
        db_ok = self._database.is_open
        db_detail = self._database.path
        if db_ok:
            try:
                schema_version = await self._database.schema_version()
            except Exception as exc:
                db_ok = False
                db_detail = f"{self._database.path}: {exc}"

        adapters = [
            AdapterHealth(
                adapter_id=descriptor.adapter_id,
                status=(await self._registry.get(descriptor.adapter_id).get_status()).value,
                capabilities=[cap.name for cap in descriptor.capabilities],
            )
            for descriptor in self._registry.list_descriptors()
        ]

        recovery_state, recovery_detail = self._recovery_health()

        components = [
            ComponentHealth(
                name="database",
                state=HealthState.OK if db_ok else HealthState.ERROR,
                detail=db_detail,
            ),
            ComponentHealth(
                name="events",
                state=HealthState.OK,
                detail=f"{self._events.subscriber_count} subscriber(s)",
            ),
            ComponentHealth(
                name="permissions",
                state=HealthState.OK,
                detail="policy active",
            ),
            ComponentHealth(
                name="recovery",
                state=recovery_state,
                detail=recovery_detail,
            ),
        ]

        if self._state is RuntimeState.RUNNING and db_ok:
            state = HealthState.OK
        elif self._state is RuntimeState.CLOSED:
            state = HealthState.STOPPED
        else:
            state = HealthState.DEGRADED

        return RuntimeHealth(
            state=state,
            version=__version__,
            uptime_seconds=self._uptime_seconds(),
            schema_version=schema_version,
            database_path=self._database.path,
            components=components,
            adapters=adapters,
        )

    def _uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now(UTC) - self._started_at).total_seconds()

    def _recovery_health(self) -> tuple[HealthState, str]:
        """Describe the last recovery pass, for ``/health``."""
        if self._recovery is None:
            return HealthState.OK, "not run yet"
        if not self._recovery:
            return HealthState.OK, "no interrupted tasks found"
        errors = [outcome for outcome in self._recovery if outcome.action == "error"]
        summary = ", ".join(f"{outcome.task_id}={outcome.action}" for outcome in self._recovery[:5])
        state = HealthState.DEGRADED if errors else HealthState.OK
        return state, f"{len(self._recovery)} task(s) reconciled at startup ({summary})"


def _default_allowed_roots(config: RuntimeConfig) -> list[Path]:
    roots = [config.data_dir]
    if config.database_path is not None:
        roots.append(Path(config.database_path).parent)
    return roots


@asynccontextmanager
async def open_runtime(config: RuntimeConfig) -> AsyncIterator[Runtime]:
    """Create, start and (on exit) close a runtime."""
    runtime = await Runtime.create(config)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()


__all__ = ["Runtime", "RuntimeState", "__version__", "open_runtime"]
