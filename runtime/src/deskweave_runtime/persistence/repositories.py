"""Repositories for the core data model.

Each repository maps between pydantic records (``deskweave_contracts``) and
SQLite rows. Repositories are the only place that knows the table shape, so the
rest of the runtime works with typed models.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from deskweave_contracts import (
    ArtifactMetadata,
    ArtifactRef,
    ErrorInfo,
    ExecutionRecord,
    NotFoundError,
    TaskRecord,
    TaskStatus,
    WorkflowRunRecord,
    WorkflowRunStatus,
)

from .database import Database, utcnow_iso


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _str_to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _error_to_str(error: ErrorInfo | None) -> str | None:
    return error.model_dump_json() if error is not None else None


def _str_to_error(value: str | None) -> ErrorInfo | None:
    return ErrorInfo.model_validate_json(value) if value else None


class WorkflowRunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, run: WorkflowRunRecord) -> WorkflowRunRecord:
        await self._db.execute(
            "INSERT INTO workflow_run (run_id, workflow, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.workflow,
                run.status.value,
                _dt_to_str(run.created_at),
                _dt_to_str(run.updated_at),
            ),
        )
        return run

    async def get(self, run_id: str) -> WorkflowRunRecord:
        row = await self._db.fetchone("SELECT * FROM workflow_run WHERE run_id = ?", (run_id,))
        if row is None:
            raise NotFoundError(f"workflow run {run_id!r} not found", details={"run_id": run_id})
        return self._row_to_model(row)

    async def update_status(self, run_id: str, status: WorkflowRunStatus) -> WorkflowRunRecord:
        await self._db.execute(
            "UPDATE workflow_run SET status = ?, updated_at = ? WHERE run_id = ?",
            (status.value, utcnow_iso(), run_id),
        )
        return await self.get(run_id)

    async def list(self, *, limit: int | None = None) -> list[WorkflowRunRecord]:
        sql = "SELECT * FROM workflow_run ORDER BY created_at"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = await self._db.fetchall(sql, params)
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: Any) -> WorkflowRunRecord:
        return WorkflowRunRecord(
            run_id=row["run_id"],
            workflow=row["workflow"],
            status=WorkflowRunStatus(row["status"]),
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
        )


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, task: TaskRecord) -> TaskRecord:
        await self._db.execute(
            "INSERT INTO task (task_id, run_id, name, status, created_at, updated_at,"
            " error, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.run_id,
                task.name,
                task.status.value,
                _dt_to_str(task.created_at),
                _dt_to_str(task.updated_at),
                _error_to_str(task.error),
                json.dumps(task.result),
            ),
        )
        return task

    async def get(self, task_id: str) -> TaskRecord:
        row = await self._db.fetchone("SELECT * FROM task WHERE task_id = ?", (task_id,))
        if row is None:
            raise NotFoundError(f"task {task_id!r} not found", details={"task_id": task_id})
        return self._row_to_model(row)

    async def update(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        error: ErrorInfo | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskRecord:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [utcnow_iso()]
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if error is not None:
            sets.append("error = ?")
            params.append(_error_to_str(error))
        if result is not None:
            sets.append("result = ?")
            params.append(json.dumps(result))
        params.append(task_id)
        await self._db.execute(
            f"UPDATE task SET {', '.join(sets)} WHERE task_id = ?", tuple(params)
        )
        return await self.get(task_id)

    async def list(
        self,
        *,
        run_id: str | None = None,
        statuses: Sequence[TaskStatus] | None = None,
        limit: int | None = None,
    ) -> list[TaskRecord]:
        sql = "SELECT * FROM task"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status.value for status in statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: Any) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            run_id=row["run_id"],
            name=row["name"],
            status=TaskStatus(row["status"]),
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
            error=_str_to_error(row["error"]),
            result=json.loads(row["result"] or "{}"),
        )


class ArtifactRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, artifact: ArtifactRef) -> ArtifactRef:
        await self._db.execute(
            "INSERT INTO artifact (artifact_id, kind, uri, media_type, size_bytes, created_at,"
            " metadata, task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.kind.value,
                artifact.uri,
                artifact.media_type,
                artifact.size_bytes,
                _dt_to_str(artifact.created_at),
                artifact.metadata.model_dump_json(),
                artifact.task_id,
            ),
        )
        return artifact

    async def get(self, artifact_id: str) -> ArtifactRef:
        row = await self._db.fetchone(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        )
        if row is None:
            raise NotFoundError(
                f"artifact {artifact_id!r} not found", details={"artifact_id": artifact_id}
            )
        return self._row_to_model(row)

    async def list(self, *, limit: int | None = None) -> list[ArtifactRef]:
        sql = "SELECT * FROM artifact ORDER BY created_at"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = await self._db.fetchall(sql, params)
        return [self._row_to_model(row) for row in rows]

    async def list_for_task(self, task_id: str) -> list[ArtifactRef]:
        """Return artifacts produced by ``task_id``, oldest first."""
        rows = await self._db.fetchall(
            "SELECT * FROM artifact WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: Any) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=row["artifact_id"],
            kind=row["kind"],
            uri=row["uri"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            task_id=row["task_id"],
            metadata=ArtifactMetadata.model_validate_json(row["metadata"] or "{}"),
            created_at=_str_to_dt(row["created_at"]),
        )


class AdapterInstallationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(
        self,
        adapter_id: str,
        *,
        name: str,
        version: str,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO adapter_installation (adapter_id, name, version, enabled, config,"
            " installed_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (adapter_id) DO UPDATE SET"
            " name = excluded.name, version = excluded.version, enabled = excluded.enabled,"
            " config = excluded.config",
            (adapter_id, name, version, int(enabled), json.dumps(config or {}), utcnow_iso()),
        )

    async def get(self, adapter_id: str) -> dict[str, Any]:
        row = await self._db.fetchone(
            "SELECT * FROM adapter_installation WHERE adapter_id = ?", (adapter_id,)
        )
        if row is None:
            raise NotFoundError(
                f"adapter installation {adapter_id!r} not found",
                details={"adapter_id": adapter_id},
            )
        return {
            "adapter_id": row["adapter_id"],
            "name": row["name"],
            "version": row["version"],
            "enabled": bool(row["enabled"]),
            "config": json.loads(row["config"] or "{}"),
            "installed_at": row["installed_at"],
        }

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall("SELECT * FROM adapter_installation ORDER BY adapter_id")
        return [
            {
                "adapter_id": row["adapter_id"],
                "name": row["name"],
                "version": row["version"],
                "enabled": bool(row["enabled"]),
                "config": json.loads(row["config"] or "{}"),
                "installed_at": row["installed_at"],
            }
            for row in rows
        ]


class PermissionGrantRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        grant_id: str,
        adapter_id: str,
        capability: str,
        scope: str,
        risk: str,
        granted_by: str,
        expires_at: str | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO permission_grant (grant_id, adapter_id, capability, scope, risk,"
            " granted_by, granted_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                adapter_id,
                capability,
                scope,
                risk,
                granted_by,
                utcnow_iso(),
                expires_at,
            ),
        )

    async def find(self, adapter_id: str, capability: str) -> dict[str, Any] | None:
        row = await self._db.fetchone(
            "SELECT * FROM permission_grant WHERE adapter_id = ? AND capability = ?"
            " ORDER BY granted_at DESC LIMIT 1",
            (adapter_id, capability),
        )
        return dict(row) if row is not None else None

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._db.fetchall("SELECT * FROM permission_grant ORDER BY granted_at DESC")
        return [dict(row) for row in rows]


class ExecutionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, record: ExecutionRecord) -> ExecutionRecord:
        await self._db.execute(
            "INSERT INTO execution_record (execution_id, adapter_id, capability, task_id, ok,"
            " error, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.execution_id,
                record.adapter_id,
                record.capability,
                record.task_id,
                int(record.ok),
                _error_to_str(record.error),
                _dt_to_str(record.created_at),
                _dt_to_str(record.finished_at),
            ),
        )
        return record

    async def list(self, *, limit: int | None = None) -> list[ExecutionRecord]:
        sql = "SELECT * FROM execution_record ORDER BY created_at"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = await self._db.fetchall(sql, params)
        return [
            ExecutionRecord(
                execution_id=row["execution_id"],
                adapter_id=row["adapter_id"],
                capability=row["capability"],
                task_id=row["task_id"],
                ok=bool(row["ok"]),
                error=_str_to_error(row["error"]),
                created_at=_str_to_dt(row["created_at"]),
                finished_at=_str_to_dt(row["finished_at"]),
            )
            for row in rows
        ]


__all__ = [
    "AdapterInstallationRepository",
    "ArtifactRepository",
    "ExecutionRepository",
    "PermissionGrantRepository",
    "TaskRepository",
    "WorkflowRunRepository",
]
