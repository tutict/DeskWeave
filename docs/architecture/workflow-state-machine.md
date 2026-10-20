# Workflow State Machine

Status: Phase 1 — still deliberately small. This is not a DAG engine.

## 1. Task vs. WorkflowRun

| Concept | Meaning | Table |
| --- | --- | --- |
| `WorkflowRun` | One execution of a named workflow (e.g. `anime-reference`). Groups tasks. | `workflow_run` |
| `Task` | One user-visible step inside a run ("collect candidates", "place reference plane"). | `task` |

A task always belongs to exactly one run (`task.run_id` → `workflow_run.run_id`,
`ON DELETE CASCADE`). Runs carry no ordering between tasks in Phase 0: the engine
records *what happened*, it does not schedule *what happens next*.

Why two levels instead of one: the UI needs "this run is running, 3 of 5 steps
done", and a retry needs to know which step failed without duplicating the run.

## 2. Task states

```
                    ┌──────────────────────► cancelled
                    │                            ▲
pending ──► running ├──► waiting_for_confirmation ┤
                    │            │               │
                    ├────────────┴──► succeeded  │
                    └──────────────► failed ─────┘
```

Allowed transitions (`runtime/src/deskweave_runtime/workflow/engine.py`):

| From | To |
| --- | --- |
| `pending` | `running`, `cancelled`, `failed` |
| `running` | `waiting_for_confirmation`, `succeeded`, `failed`, `cancelled` |
| `waiting_for_confirmation` | `running`, `succeeded`, `failed`, `cancelled` |
| `succeeded` / `failed` / `cancelled` | — (terminal) |

Anything else raises `LifecycleError` with `details={"from": ..., "to": ...}`.
Terminal states are truly terminal: "retry" creates a new task, it does not
resurrect a failed one. This keeps the audit trail append-only.

## 3. Run states

`pending → running → succeeded | failed | cancelled`, with the same
terminal-state rule. `start_run()` creates a run directly in `running`; the
`pending` state exists for runs created ahead of execution (Phase 1).

## 4. User confirmation nodes

`waiting_for_confirmation` is the seam for "the agent proposed, the human
decides". A task enters it when an operation requires consent; it leaves it in one
of three ways:

- `succeeded` — the user confirmed and the work was already done optimistically
  (only valid for reversible work);
- `running` — the user confirmed and execution continues;
- `cancelled` / `failed` — the user rejected, or the confirmation timed out.

Phase 1 implements this seam for the reference workflow
(`runtime/src/deskweave_runtime/references/service.py`):

| Situation on `confirm(task_id, selection)` | Outcome |
| --- | --- |
| task is `waiting_for_confirmation`, selection belongs to it | `running` → `succeeded` / `failed` |
| task is `succeeded` **and** already has a confirmation | the stored result is replayed; nothing runs twice |
| task is `running` with a confirmation | `conflict` ("confirmation is already being executed") |
| task is `pending`, `running` without confirmation, `failed` or `cancelled` | `conflict` (`details.status`) |
| an artifact id does not belong to this task | `permission_denied` (`details.artifact_ids`) |
| `target` is not the configured adapter | `validation_error` (`details.allowed`) |

Implementation rules that make the table true:

- `confirm()` holds a **per-task `asyncio.Lock`**, so two clicks cannot both pass
  the state check.
- The state write (`waiting_for_confirmation → running`) happens *before* the
  adapter call; the terminal write happens in all cases, including a crash inside
  the target (which is caught and turned into `execution.ok = false`).
- The target is only reachable through a one-shot permission allowance recorded by
  `confirm()` itself — there is no other code path that can call it.
- Confirmation data (`artifact_ids`, `target`, `plane_name`, `confirmed_at`) is
  stored in `task.result.confirmation`; no image content or token is recorded.

Permission-level confirmation (see the permission layer) and workflow-level
confirmation are two different mechanisms: the former guards a single adapter
call, the latter guards a step in a multi-step plan. Both are required here, and
neither can be satisfied from the agent side.

## 5. Failure, cancellation, recovery

- **Failure**: `set_task_status(task_id, FAILED, error=ErrorInfo(...))` persists the
  structured error in `task.error`; the run is finished with `failed` by the caller.
- **Cancellation**: `cancel_task()` moves a task to `cancelled`. In Phase 1 it also
  cancels the background `asyncio` task that owns the collection and awaits it, so
  the browser work stops instead of running to completion. A task whose confirmed
  target execution is already in flight is **not** cancellable (`conflict`) —
  killing Blender mid-write would leave a file nobody can verify.
- **Recovery**: because state lives in SQLite, a restarted runtime can list runs
  and tasks and resume from the last persisted state. A run left in `running`
  after a crash is visible, not silently retried; the UI shows it as running and
  the user can start a new collection.
- **Idempotency**: `create_task()` requires an existing run; creating the same
  task twice produces two tasks. Within one task, `confirm` is idempotent (see §4).
  A retried *collection* is a new task, never a mutation of the old one.

## 6. Persistence requirements

- Writes go through repositories (`TaskRepository`, `WorkflowRunRepository`) inside
  `runtime/src/deskweave_runtime/persistence/`; nothing else issues SQL for these
  tables. Plugins never write these tables.
- `status` columns carry `CHECK` constraints (`design` intent: an invalid state
  cannot be stored even if application code has a bug).
- `task.result` is a JSON object (default `{}`), `task.error` holds the serialised
  `ErrorInfo`; both are read back into pydantic models.
- Timestamps are ISO-8601 UTC strings, generated by the engine, not the database.
- Every transition emits an event (`task_status_changed`,
  `workflow_run_status_changed`) on the in-process bus. `/events` exposes the
  bounded history; the Phase 1 UI polls `/tasks` every 2 s while a task is active
  instead of subscribing, because a push channel would need its own auth story.

## 7. What is intentionally missing

- No dependency graph, no fan-out/fan-in, no conditional edges.
- No scheduler, no worker pool, no retry policy.
- No compensation / rollback semantics.

Adding those before a real workflow exists would encode guesses into the schema.
The transition table above is the part that must not change silently.
