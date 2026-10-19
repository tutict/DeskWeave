# deskweave-runtime

The local-first Python core of DeskWeave.

Responsibilities:

- **core** — runtime lifecycle, configuration, health.
- **workflow** — task / workflow-run state machine (not a general DAG engine).
- **permissions** — a single permission model shared by every adapter.
- **artifacts** — logical artifact references with allowed-root enforcement.
- **events** — in-process event bus.
- **persistence** — SQLite schema, versioned migrations and repositories.
- **service** — a token-gated, loopback-only HTTP control API used by the
  desktop shell.

The runtime never starts external processes on import and never creates global
runtime instances at import time.
