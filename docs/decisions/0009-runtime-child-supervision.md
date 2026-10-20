# ADR 0009 — Supervising the Python runtime child process

- Status: accepted
- Date: 2026-10-21
- Phase: 1.5

## Context

The desktop shell is a Tauri (Rust) process that spawns one Python process
(`python -m deskweave_runtime.service`) and talks to it over loopback HTTP. That
child in turn spawns a Playwright browser and headless Blender runs.

Three lifecycle facts measured on this machine (Windows 11, Python 3.12.9 in a
uv-managed venv):

1. `.venv/Scripts/python.exe` is a **launcher that re-execs the real
   interpreter**: `Popen.pid` is 19804 while `os.getpid()` inside the child is
   5440. Killing `Popen.pid` leaves the interpreter running — the runtime keeps
   its port and the next start fails with "address in use".
2. Closing the Tauri window does not, by itself, terminate a process tree; a
   panic in the shell would otherwise leave the runtime (and any Blender run it
   started) alive forever.
3. The runtime has its own graceful shutdown (`POST /shutdown` → the service
   stops the HTTP server, cancels background work and closes SQLite). Killing it
   before that leaves an unfinished database write window.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + graceful request + tree kill** | Kernel-enforced: the whole tree dies with the shell, even on a panic. Children (Blender, Playwright) are covered because they inherit the job | Windows-only primitive; the shell must create the job before the child can escape it |
| `taskkill /IM python.exe /F` | One line | Kills **other** Python processes on the machine (the user's notebooks, other apps) — unacceptable |
| `child.kill()` on the `Popen` handle | Portable | Orphans the re-exec'd interpreter (fact 1) |
| Let the runtime exit when the port closes | No code | Never happens: the runtime owns the port, not the other way round |
| Watchdog process that polls a heartbeat | Portable | Another moving part, and it has the same orphan problem when *it* dies |

## Decision

1. **Job Object, assigned immediately after spawn.**
   `process.rs` creates the job before starting the child
   (`CreateJobObjectW` + `SetInformationJobObject(KILL_ON_JOB_CLOSE)`), then
   assigns the child right after `Command::spawn()`. Between those two calls the
   child is unassigned; if it had already spawned a grandchild in that window the
   grandchild would escape. The window is a few microseconds of interpreter
   startup (no Blender, no browser yet), it is logged
   (`could not add the runtime to the job object`), and `runtime_status.supervised`
   reports whether the assignment actually happened instead of assuming it did.
2. **Graceful first, forced last.** `RuntimeProcess::stop_with_grace(5s)`:
   `POST /shutdown` with the token → poll `try_wait()` every 50 ms → after the
   deadline `taskkill /PID <pid> /T /F` (scoped to *this* pid, never a name) →
   `wait()`. The outcome is an enum (`AlreadyExited` / `Graceful` / `Forced`) and
   is logged; "it exited" is never inferred from "we sent a signal".
3. **The job handle is released explicitly**, and `Drop::close` on the handle is
   what kills the tree. On a normal exit the child is already gone, so closing the
   job is a no-op.
4. **No other process is ever a target.** There is no name-based matching anywhere
   in the supervision code. The tree kill is scoped to the pid the shell itself
   created.

## Consequences

- A shell crash or a killed desktop process cannot leave the runtime (or a Blender
  run) behind on Windows; the kernel does the cleanup.
- `runtime_status.supervised == false` is an actionable signal: the shell can tell
  the user that a crash could orphan the runtime, instead of hiding it.
- **Platform gap, documented rather than papered over:** Job Objects do not exist
  on macOS/Linux. There `noop::ChildJob` reports `Unsupported`, the shell relies on
  the graceful request plus `kill()` of the direct child, and `supervised` is
  `false`. On macOS/Linux the venv launcher does not re-exec, so `kill()` targets
  the real interpreter; the remaining gap is a Blender run outliving a hard kill.
- Tests (`cargo test --lib`): `closing_the_job_kills_its_members` proves the
  kernel-level guarantee; `kill_tree_terminates_the_launcher_and_its_child` proves
  the re-exec case; `stop_escalates_to_kill_when_the_endpoint_is_unreachable` and
  `stop_is_idempotent` cover the escalation and the double-shutdown path.
- An optional cleanup timeout of 5 s means a hung runtime delays app exit by 5 s at
  worst before the forced kill. That is the deliberate trade: losing the SQLite
  close is worse than a slow quit.
