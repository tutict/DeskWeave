# ADR 0011 — What happens to tasks that were running when the process died

- Status: accepted
- Date: 2026-10-21
- Phase: 1.5

## Context

A task is a row in SQLite plus zero or more artifacts in the artifact store. Three
processes are involved in the dangerous window: the runtime (Python), Blender, and
the filesystem. There is no distributed transaction between them, so the following
states are all reachable after a crash:

| Interruption | Durable state left behind |
| --- | --- |
| Killed during the browser download | `running`, some image artifacts registered, `candidates` not written |
| Killed after `create_task`, before `set_task_status(running)` | `pending` |
| Killed after the user confirmed, while Blender runs | `running` + `result.confirmation`, no `execution` |
| Blender wrote the `.blend`, the runtime died before persisting | same as above, but the file exists on disk |
| Blender finished and verification failed | `failed` (terminal, nothing to recover) |

A naive recovery ("mark every `running` task failed", or "re-run everything that
looks unfinished") is wrong in both directions: it turns a finished Blender run into
a reported failure, and it re-runs a destructive operation the user never
authorised after the restart.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Evidence-based reconciliation at startup, per state** | Every terminal decision rests on something observable (an artifact row, a re-opened `.blend`); no execution without a human | Needs an adapter hook to locate/verify its own output |
| Mark all non-terminal tasks `failed` | Simple, no ambiguity | Discards downloaded images; reports a completed Blender run as a failure |
| Resume automatically (re-run the collection / re-run Blender) | "Just works" | Re-executes a high-risk operation without a fresh confirmation — forbidden |
| New `interrupted` state | Explicit in the UI | Extends the state machine for a condition that existing states already express; the UI would need a new branch for no new information |
| Recover lazily on first read | Startup stays fast | Two code paths decide the same thing, and the first read happens inside a request |

## Decision

Reconcile once at startup, before the control API serves requests, and only where
evidence exists (`runtime/src/deskweave_runtime/references/recovery.py`):

| Recorded state | Evidence found | Action | Result |
| --- | --- | --- | --- |
| `pending` | — | `never_started` | `failed`, `error.code="interrupted"` |
| `running`, no confirmation | ≥1 image artifact for the task | `resumed_partial_collection` | `waiting_for_confirmation` with those candidates |
| `running`, no confirmation | none | `collection_interrupted` | `failed`, `error.code="interrupted"` |
| `running` + confirmation | `execution` recorded (defensive) | `recorded_execution` | state derived from `execution.ok` |
| `running` + confirmation | output file found **and** re-verified | `verified_output` | `succeeded`, artifact registered with `execution.recovered=true` |
| `running` + confirmation | file found, verification failed | `confirmation_required` | back to `waiting_for_confirmation`, nothing executed |
| `running` + confirmation | file found, adapter cannot verify | `confirmation_required` | same |
| `running` + confirmation | no target adapter registered | `confirmation_required` | same |
| `running` + confirmation | no file | `confirmation_required` | same |
| `waiting_for_confirmation` | — | *untouched* | stays parked |
| terminal states | — | *untouched* | — |

Rules that make the table safe:

- **Nothing is executed.** Recovery never calls an adapter's `execute`; the only
  adapter interaction is `find_output` (a path derivation plus a `stat`) and
  `verify_output` (re-open the file in the target application). A retry can only
  happen when the user confirms again, which is exactly the authorisation the
  original confirmation gave and a fresh one does not assume.
- **A file existing is not success.** `verify_output` re-opens the scene and checks
  that it contains one textured, non-degenerate object per requested artifact;
  anything else returns the task to the confirmation gate with a reason.
- **Every decision is recorded** in `task.result.recovery`
  (`{action, detail, at}`), logged with the task id, and summarised in
  `/health` (`components[name=recovery]`), so "what did the restart do?" is
  answerable without reading the database.
- **Bounded and idempotent.** At most 20 tasks per start (a full batch logs a
  warning and the rest are handled next start), 15 s to locate an output, 90 s to
  verify one. Running recovery twice changes nothing: only `pending`/`running`
  rows are queried, and the first pass moved them out of those states.
- **A failure to reconcile never blocks startup.** Each task is isolated; an
  exception becomes a `RecoveryOutcome(action="error")` and the app still opens.

## Consequences

- The UI shows every recovered task at a state the user can act on
  (`waiting_for_confirmation` with a note, or `failed` with an actionable message),
  instead of an eternal spinner.
- The adapter SDK grew one optional protocol,
  `RecoverableOutput` (`find_output` / `verify_output`). Adapters that do not
  implement it are handled by the "ask the user" row of the table, so a third-party
  connector degrades safely.
- `task.result.request` is now written **before** the collection starts, so a task
  interrupted mid-download still knows what was being collected.
- The `.blend` path remains inside the adapter (derived from task id + plane name);
  the runtime never learns it and never stores it.
- Tests: `tests/unit/test_recovery.py` (12 cases, including "an unconfirmed task is
  never executed by recovery"), and `tests/integration/test_recovery_service.py`
  which seeds a real interrupted database and restarts the real service process,
  asserting `/health`, `/tasks/{id}` and the log line.
