# ADR 0005 — SQLite for local persistence

- Status: accepted
- Date: 2026-10-19
- Phase: 0

## Context

The runtime must survive restarts with tasks, runs, artifacts, adapter
installations, permission grants and execution records intact. The data is small
(thousands of rows), single-user, and must not require a daemon.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **SQLite (aiosqlite)** | Zero-ops, file-based, transactional, WAL gives concurrent readers, ships with Python | Single writer; schema changes need migrations; no built-in row-level security |
| JSON files on disk | Trivial | No transactions, no constraints, corruption on partial writes, painful querying |
| DuckDB | Great analytics | Wrong shape for transactional writes; large binary |
| Postgres / Redis | Strong semantics | Requires a service the user must install and keep running — disqualifying for local-first |

## Decision

Use **SQLite** through **aiosqlite**, one connection owned by the runtime, with a
**hand-rolled, versioned migration list** (`runtime/src/deskweave_runtime/persistence/migrations.py`).

Schema v1 creates six tables: `workflow_run`, `task`, `artifact`,
`adapter_installation`, `permission_grant`, `execution_record`, plus
`schema_migrations`. `PRAGMA foreign_keys = ON` and `journal_mode = WAL` are set on
open.

## Reasons

1. Constraints belong in the database: `CHECK` on status columns means an invalid
   state cannot be persisted even if application code regresses.
2. A migration table with `(version, name, applied_at)` makes the schema version
   observable — the health endpoint reports it.
3. WAL allows the control API thread to read while the runtime writes.
4. No daemon to install, no port to secure, no credentials to store.

## Consequences

- Writes are serialised. Fine for one runtime; would need a queue if adapters
  start writing at high frequency.
- Migrations are forward-only and applied at startup; a failed migration rolls
  back that migration and raises `PersistenceError`. There is no downgrade path —
  restoring from a backup is the documented recovery.
- Schema changes require a new `Migration` entry; editing an applied migration is
  forbidden (it would not re-run, and CI cannot detect a mismatch on machines that
  already applied it).
- Timestamps are ISO-8601 strings, not SQLite `datetime` — keeps round-trips
  explicit and avoids timezone surprises.
- Sensitive credentials are *not* stored: grants record who/when/scope, not
  secrets. Adapter config may be stored as JSON, so connectors must not put tokens
  in it.

## Revisit if

- Concurrent writer pressure becomes real (move to a write queue), or
- the dataset outgrows a single file (unlikely for this product), or
- per-row access control is required (then a real database with roles, not SQLite).
