"""Versioned SQLite migrations.

Each :class:`Migration` is applied at most once and recorded in the
``schema_migrations`` table, so the schema version is always traceable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


_INITIAL_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE workflow_run (
        run_id      TEXT PRIMARY KEY,
        workflow    TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (
            status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
        ),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE task (
        task_id     TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL REFERENCES workflow_run (run_id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (
            status IN (
                'pending', 'running', 'waiting_for_confirmation',
                'succeeded', 'failed', 'cancelled'
            )
        ),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        error       TEXT,
        result      TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_task_run_id ON task (run_id)",
    "CREATE INDEX idx_task_status ON task (status)",
    """
    CREATE TABLE artifact (
        artifact_id TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        uri         TEXT,
        media_type  TEXT,
        size_bytes  INTEGER,
        created_at  TEXT NOT NULL,
        metadata    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE adapter_installation (
        adapter_id   TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        version      TEXT NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        config       TEXT NOT NULL DEFAULT '{}',
        installed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE permission_grant (
        grant_id    TEXT PRIMARY KEY,
        adapter_id  TEXT NOT NULL,
        capability  TEXT NOT NULL,
        scope       TEXT NOT NULL,
        risk        TEXT NOT NULL,
        granted_by  TEXT NOT NULL,
        granted_at  TEXT NOT NULL,
        expires_at  TEXT
    )
    """,
    "CREATE INDEX idx_permission_grant_lookup ON permission_grant (adapter_id, capability)",
    """
    CREATE TABLE execution_record (
        execution_id TEXT PRIMARY KEY,
        adapter_id   TEXT NOT NULL,
        capability   TEXT NOT NULL,
        task_id      TEXT,
        ok           INTEGER NOT NULL,
        error        TEXT,
        created_at   TEXT NOT NULL,
        finished_at  TEXT
    )
    """,
    "CREATE INDEX idx_execution_record_adapter ON execution_record (adapter_id)",
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial_schema", statements=_INITIAL_SCHEMA),
)


LATEST_VERSION: int = max(migration.version for migration in MIGRATIONS)


__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration"]
