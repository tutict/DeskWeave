# ADR 0004 — Local process communication

- Status: accepted
- Date: 2026-10-19
- Phase: 0

## Context

The UI (Rust + WebView) and the core (Python) are separate processes. We need a
transport that is simple, debuggable with `curl`, and safe by default on a machine
that may be running untrusted browser content at the same time.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **HTTP/1.1 on 127.0.0.1 + Bearer token** | Trivial to debug; language-neutral; no extra server dependency (stdlib `ThreadingHTTPServer`); token + loopback is a small, well-understood surface | Port allocation, token lifecycle, and a real (if tiny) attack surface |
| Unix domain socket / Windows named pipe | No TCP port at all; filesystem ACLs as auth | Poor cross-platform parity; harder to debug; needs a custom client in Rust and Python |
| stdio pipes from Rust to Python | No network surface | One channel for both control and logs; request/response correlation must be hand-rolled; the Python process is tied to one parent |
| gRPC / protobuf | Typed, streaming | Codegen, HTTP/2 and a large dependency for a local single-user app |

## Decision

Use **HTTP on the loopback interface** with a **per-run random token**.

- The Rust shell picks a free port (`bind(127.0.0.1:0)`), generates a UUID token,
  and passes both to the child via environment variables.
- `RuntimeConfig` rejects any non-loopback host at validation time; there is no
  configuration that exposes the API to the network.
- Every request must present `Authorization: Bearer <token>` or
  `X-DeskWeave-Token`; comparison uses `hmac.compare_digest`.
- The webview never sees the token. It calls one Rust command,
  `runtime_request(path)`, which accepts only `/health`, `/adapters`, `/tasks`,
  `/events`. This is the anti-SSRF boundary: a compromised renderer cannot use
  the shell as a general local proxy.
- `/shutdown` exists for graceful stop; the shell also kills the child on exit.

For CLI/headless use, the service writes `runtime.json` (host, port, pid, token)
into its data directory and can print a one-line JSON handshake with `--announce`.
The handshake is what the e2e test uses.

## Reasons

1. `curl` debugging is worth a lot during the first months of an infrastructure project.
2. Loopback + token is a real boundary, not a fig leaf, as long as the token stays
   out of the webview and out of logs.
3. No new server dependency: stdlib HTTP keeps the runtime importable in tests.
4. It is the same shape the UI would use if the core ever moved to another machine
   (which is explicitly *not* a goal, but the escape hatch is cheap).

## Consequences

- **Known limitation:** the token is stored in plaintext in `<data_dir>/runtime.json`
  so that local CLI tools can find it. Any process running as the same OS user can
  read it. This is acceptable for a single-user desktop app but must be revisited
  before multi-user or shared-machine use.
- Abrupt termination (SIGKILL / Task Manager) can leave the Python child alive;
  the normal exit path kills it. A job object on Windows / `prctl` on Linux is the
  Phase 1 hardening.
- There is a small race between choosing a free port and the child binding it;
  it surfaces as a clear startup error, not a silent failure.
- No TLS. Correct for loopback; would be wrong the moment the bind address changes.

## Revisit if

- The runtime needs to serve more than the local user, or
- the token file proves insufficient against local same-user malware (move the
  token to an OS keychain and pass it only through inherited handles).
