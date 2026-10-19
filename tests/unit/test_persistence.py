"""Unit tests for SQLite initialization, migrations and persistence."""

from __future__ import annotations

from pathlib import Path

from deskweave_contracts import TaskRecord, TaskStatus, WorkflowRunRecord, WorkflowRunStatus
from deskweave_runtime.persistence import (
    LATEST_VERSION,
    Database,
    TaskRepository,
    WorkflowRunRepository,
)


async def test_schema_version_is_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    try:
        assert await db.schema_version() == LATEST_VERSION
    finally:
        await db.close()


async def test_data_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "test.db"

    db = Database(path)
    await db.initialize()
    runs = WorkflowRunRepository(db)
    await runs.create(
        WorkflowRunRecord(run_id="r1", workflow="demo", status=WorkflowRunStatus.RUNNING)
    )
    tasks = TaskRepository(db)
    await tasks.create(TaskRecord(task_id="t1", run_id="r1", name="collect"))
    await db.close()

    reopened = Database(path)
    await reopened.initialize()
    try:
        run = await WorkflowRunRepository(reopened).get("r1")
        assert run.workflow == "demo"
        task = await TaskRepository(reopened).get("t1")
        assert task.status is TaskStatus.PENDING
    finally:
        await reopened.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.close()
    await db.close()
    assert not db.is_open


async def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.initialize()
    assert await db.schema_version() == LATEST_VERSION
    await db.close()


async def test_task_update_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    try:
        runs = WorkflowRunRepository(db)
        await runs.create(WorkflowRunRecord(run_id="r1", workflow="demo"))
        tasks = TaskRepository(db)
        await tasks.create(TaskRecord(task_id="t1", run_id="r1", name="collect"))
        updated = await tasks.update("t1", status=TaskStatus.RUNNING, result={"n": 1})
        assert updated.status is TaskStatus.RUNNING
        assert updated.result == {"n": 1}
    finally:
        await db.close()
