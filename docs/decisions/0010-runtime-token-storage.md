# ADR 0010 — Where the runtime token lives (and how the old one is removed)

- Status: accepted
- Date: 2026-10-21
- Phase: 1.5

## Context

The control API on `127.0.0.1` is the only way the desktop UI reaches the
runtime, and every request must carry a shared token. A loopback port is not an
authentication boundary: any local process can connect to it.

Phase 1 wrote the token to `<data_dir>/runtime.json` next to the port so that
tooling could find the service. That file is a plain-text credential readable by
**any process running as the same user** — including a malicious script that never
needs to guess a port. It also survived restarts, so a token leaked once stayed
valid forever.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Token generated in the shell, passed to the child through its environment, never persisted** | Nothing on disk to leak; a new token every start; the webview never sees it (Rust owns it) | Only works while the shell is the parent — a manually started runtime needs the token passed in |
| Windows Credential Manager (`CredWrite`) / Keychain | OS-protected at rest | Adds a platform-specific dependency and a read-back path for a secret that does not need to persist at all: the token is per-run, and the only consumer is the parent process |
| Keep the token in `runtime.json` with restrictive ACLs | Trivial to implement | Same-user processes still read it; ACLs differ per platform and are easy to get wrong silently |
| No token, loopback only | Simplest | Any local process could drive Blender through the API |
| Per-request signature (HMAC over the body) | Replay-resistant | More moving parts than a desktop app with one client needs now |

## Decision

1. **The token is generated per run by the Rust shell** (`Uuid::new_v4()`), passed
   to the child as `DESKWEAVE_TOKEN` in its environment (not in `argv`: command
   lines are visible to other users through the process list), and kept only in
   `RuntimeBridge`'s memory. The Python service also accepts `--token` for manual
   use and generates one if neither is provided.
2. **`runtime.json` records only `host`, `port`, `url`, `pid`.** It is a discovery
   file, not a credential store.
3. **Migration is idempotent and never breaks startup.** On every start the service
   reads the previous `runtime.json`; if it contains a `token` key it logs
   `removed a legacy plaintext token …` and rewrites the file without it. A missing
   file is normal; a corrupt one is logged (`ignoring an unreadable …`) and
   overwritten. A broken cache file must never prevent the runtime from starting.
4. **No credential store is used, deliberately.** Persisting the token would create
   a second place to leak it for zero benefit: the token's only consumer is the
   parent process, which already holds it in memory.
5. **The webview never receives the token.** `runtime_request` and
   `artifact_preview` in `bridge.rs` attach `Authorization: Bearer …` on the Rust
   side; the IPC surface returns runtime JSON only. Browser dev mode
   (`VITE_DESKWEAVE_RUNTIME_TOKEN`) is a development convenience, documented as
   such — it is not how the desktop app works.

## Consequences

- Restarting the runtime invalidates the previous token: a stale client gets `401`,
  not access. There is no rotation endpoint, because rotation *is* restart.
- `POST /shutdown` also requires the token, so a hostile local process cannot stop
  the runtime without it.
- `runtime.json` leaking is now harmless (host/port/pid are discoverable anyway).
- Tests: `tests/e2e/test_service_cli.py` asserts the file has no `token` key and
  that a wrong token still returns 401; `tests/integration/test_state_file.py`
  pins both migration cases (legacy token removed, corrupt file tolerated) against
  the real service process.
- **Remaining risk, stated plainly:** environment variables are readable by a
  same-user process with debug privileges, and the token is in the child's
  environment for the whole run. On a machine where the user's own account is
  hostile to itself, nothing in this design helps; the boundary is "same user =
  trusted", which matches the product (a local desktop tool driving the user's own
  Blender).
