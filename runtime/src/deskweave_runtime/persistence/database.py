"""SQLite database handle with versioned migrations.

A single :class:`Database` owns one ``aiosqlite`` connection for the lifetime of
the runtime. It is created explicitly (never at import time) and must be closed
via :meth:`Database.close`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from deskweave_contracts import PersistenceError

from .migrations import MIGRATIONS

_MEMORY = ":memory:"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Owns the SQLite connection and applies migrations on initialize."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise PersistenceError("database is not initialized")
        return self._conn

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def initialize(self) -> None:
        """Open the connection and apply pending migrations (idempotent)."""
        if self._conn is not None:
            return
        if self._path != _MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = await aiosqlite.connect(self._path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            if self._path != _MEMORY:
                await conn.execute("PRAGMA journal_mode = WAL")
            self._conn = conn
            await self._apply_migrations()
        except PersistenceError:
            raise
        except Exception as exc:
            await self.close()
            raise PersistenceError(
                f"failed to initialize database at {self._path!r}: {exc}",
                details={"path": self._path},
            ) from exc

    async def close(self) -> None:
        """Close the connection. Safe to call multiple times."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def schema_version(self) -> int:
        conn = self.connection
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        async with conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["version"]) if row is not None else 0

    async def _apply_migrations(self) -> None:
        conn = self.connection
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        await conn.commit()
        current = await self.schema_version()
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            try:
                for statement in migration.statements:
                    await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, utcnow_iso()),
                )
                await conn.commit()
            except Exception as exc:
                await conn.rollback()
                raise PersistenceError(
                    f"migration {migration.version} ({migration.name}) failed: {exc}",
                    details={"version": migration.version, "name": migration.name},
                ) from exc

    # -- low level helpers -------------------------------------------------
    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        await self.connection.execute(sql, parameters)
        await self.connection.commit()

    async def fetchone(self, sql: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.connection.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.connection.execute(sql, parameters) as cursor:
            return list(await cursor.fetchall())


__all__ = ["Database", "utcnow_iso"]
