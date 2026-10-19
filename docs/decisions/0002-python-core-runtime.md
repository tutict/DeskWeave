# ADR 0002 — Python core runtime

- Status: accepted
- Date: 2026-10-19
- Phase: 0

## Context

The core has to talk to three ecosystems: MCP (Python SDK is the reference
implementation), Playwright (first-class Python bindings), and Blender (`bpy`,
which only exists inside Blender's embedded Python). Durable state, permissions
and workflow transitions must live somewhere with a stable process lifetime.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Python service** | Native access to `bpy`-adjacent tooling, Playwright and the MCP SDK; asyncio fits the IO-bound workload; easy for contributors | Slower than Rust/Go; GIL limits CPU-bound work; packaging a Python runtime into a desktop bundle is work |
| Rust/Go core with Python adapters | Fast, single binary | Two runtimes plus an IPC layer; `bpy` still forces a Python process anyway; slower iteration |
| Node.js core | Same language as the UI | Playwright is fine, `bpy` is not; MCP SDK parity is weaker; no real gain over Python |
| In-process inside the Tauri binary | No IPC | Blender cannot be embedded that way; crash in core kills the UI |

## Decision

Implement the core as a standalone **Python ≥ 3.12** package (`deskweave-runtime`)
in a **uv workspace**, exposed through a loopback HTTP control API, and keep the
Tauri process free of business logic.

Python 3.12 is the floor because the code uses `StrEnum`, `datetime.UTC` and modern
typing syntax. `uv` manages the workspace and lockfile so installs are reproducible.

## Reasons

1. `bpy` can only run inside Blender's Python — a Python core is on the path anyway.
2. MCP's reference SDK is Python; protocol bugs get fixed upstream fastest there.
3. asyncio matches the workload: subprocess supervision, HTTP, browser automation.
4. A separate process means a core crash is recoverable without losing the window,
   and the core can be run headless for tests and for agents.

## Consequences

- Two toolchains (Python + Node/Rust) in one repo; CI must cover both.
- Distributing the app means shipping or discovering a Python interpreter. Phase 0
  resolves `.venv` or `DESKWEAVE_PYTHON` and reports a clear error otherwise;
  bundling an interpreter is a later task.
- The HTTP boundary forces explicit serialisation — which is a feature: it is the
  same shape agents and the UI consume.
- CPU-heavy work (image processing) will need a worker/process split later rather
  than threads.

## Revisit if

- Startup latency or memory of the Python child becomes unacceptable on target
  hardware, or
- a required dependency has no maintained Python binding.
