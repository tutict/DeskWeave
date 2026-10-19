# DeskWeave System Architecture

Status: Phase 0 (engineering foundation).
Audience: contributors and anyone integrating an agent with DeskWeave.

## 1. Goals

DeskWeave is **local-first agent desktop infrastructure**. It connects AI agents,
desktop applications, workflows and the user in one place, on the user's machine,
without a hosted control plane.

Phase 0 delivers the skeleton that later phases build on:

| Goal | How Phase 0 satisfies it |
| --- | --- |
| A stable contract for desktop connectors | `deskweave-adapter-sdk` with a single `execute()` template |
| One runtime that owns lifecycle and state | `deskweave-runtime` (`Runtime`, SQLite, event bus) |
| A standard way for agents to connect | MCP server over stdio (`deskweave-mcp-gateway`) |
| A UI that shows real, not simulated, state | Tauri 2 + Vue 3 shell reading the runtime health API |
| Safety by default | loopback-only control API, token auth, permission layer, allowed roots |

Non-goals for this phase: a general DAG engine, a plugin marketplace, remote
multi-user operation, and any real browser or Blender automation.

## 2. Components

```
┌──────────────────────────────── desktop process ────────────────────────────────┐
│  Tauri 2 shell (Rust)                                                           │
│    • window / system integration                                                │
│    • spawns and supervises the Python runtime child process                     │
│    • holds the control-API token; exposes only `runtime_request(path)`          │
│  ┌─────────────────────────── WebView ────────────────────────────┐             │
│  │  Vue 3 + TypeScript UI  (apps/desktop/src)                     │             │
│  │  overview · tasks · references · logs · settings               │             │
│  └────────────────────────────────────────────────────────────────┘             │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │ HTTP, 127.0.0.1 only, Bearer token
┌────────────────────────────────────▼────────────────────────────────────────────┐
│  Python core runtime  (runtime/src/deskweave_runtime)                           │
│                                                                                 │
│  Runtime ──┬── RuntimeConfig            (validated, loopback-only host)         │
│            ├── AdapterRegistry          (register / lookup / unregister)        │
│            ├── PermissionManager        (single policy for every adapter)        │
│            ├── WorkflowEngine           (runs, tasks, legal transitions)        │
│            ├── ArtifactStore            (logical ids + allowed roots)           │
│            ├── EventBus                 (in-process pub/sub, bounded history)   │
│            └── Database                 (aiosqlite + versioned migrations)      │
│                                                                                 │
│  Control API (service/)  ── stdlib ThreadingHTTPServer on a background thread   │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │ same runtime instance, no extra process
┌────────────────────────────────────▼────────────────────────────────────────────┐
│  MCP gateway (packages/mcp-gateway)                                             │
│  stdio transport · read-only tools · 1:1 with one runtime                       │
└─────────────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │ MCP (stdio)
                                Codex / agents
```

Adapters are *not* processes in Phase 0. `browser`, `blender` and `filesystem`
are in-process connectors registered with the runtime. Splitting an adapter into
its own process is allowed later (see `docs/decisions/0004-local-process-communication.md`).

## 3. Dependency direction

```
deskweave-contracts          (pure data models + error taxonomy; depends on nothing)
      ▲        ▲
      │        │
plugin-sdk   adapter-sdk ──────► contracts
      ▲        ▲
      │        │
      │     adapters/{browser,blender,filesystem}
      │        ▲
      │        │
    runtime ───┘  ──────────────► contracts, adapter-sdk
      ▲
      │
mcp-gateway ───────────────────► runtime, contracts

plugins/anime-modeling ────────► plugin-sdk, adapters/*
```

Rules enforced by review (and by the package layout):

1. `contracts` has **no** internal dependencies. It is safe to import anywhere.
2. `runtime` never imports `mcp-gateway`, adapters or plugins.
3. `mcp-gateway` never imports adapters; it only calls `Runtime`.
4. Adapters never import workflow/business logic and never touch the database or
   the gateway internals.
5. Plugins never write core tables; they act through `PluginContext`.
6. The webview never talks to Python directly — only through the Rust bridge.

## 4. Data flow (reference-image scenario, target state)

```
Agent (Codex)
  │ MCP tool call: (future) collect_reference_images
  ▼
MCP gateway ──► Runtime.execute(adapter_id, request)
                   │ 1. capability resolved
                   │ 2. arguments validated against JSON Schema
                   │ 3. PermissionManager.check (grant / read-only / confirm)
                   │ 4. adapter._run(...)
                   │ 5. ExecutionRecord persisted (audit)
                   ▼
                BrowserAdapter → candidate images (Artifacts)
                   │
                   ▼
                UI: user selects images  (Phase 1)
                   │
                   ▼
                BlenderAdapter.create_reference_plane(...)  (Phase 1)
```

In Phase 0 only steps 1–5 exist, plus `ArtifactStore.register` and the workflow
status machine. The browser and Blender adapters return
`NotImplementedYetError` rather than fake success.

## 5. Process boundaries

| Process | Role | Lifecycle |
| --- | --- | --- |
| Desktop shell (Rust) | window, system integration, child-process supervisor | started by the user |
| Python runtime | core state, control API, MCP server | spawned by the shell (or manually in dev) |
| Browser / Blender | external apps | out of scope in Phase 0 |

There is no message broker, no Redis, no Kubernetes. One machine, at most a
handful of processes.

## 6. Agent ↔ application relationship

Agents see **tools**, not Python objects. The MCP gateway maps a tool call to a
runtime method; the runtime applies validation and permissions. An agent cannot
name an adapter method that was not declared as a capability, and cannot pass a
path that escapes the allowed roots.

Phase 0 exposes only read-only tools. Any future side-effecting tool must go
through `Runtime.execute`, which is where the permission layer lives — the
gateway must never call an adapter directly.

## 7. MVP scope

In scope (implemented):

- workspace layout, packaging and typing for five Python distributions;
- runtime create/start/close, adapter registry, health report;
- Adapter SDK with schema validation, timeout, structured errors;
- permission model + grant persistence, default deny for non-read-only;
- SQLite schema with migrations for task, run, artifact, installation, grant, execution;
- in-process event bus with bounded history;
- MCP server over stdio with four read-only tools and protocol tests;
- Tauri 2 + Vue 3 desktop shell showing real health/adapters/tasks;
- CI for Python lint+test and frontend typecheck+build+test.

Out of scope (explicitly deferred): real Playwright automation, bpy execution,
user confirmation UI flow, screenshot storage, sandboxing, Windows/macOS packaging
signing, remote access.

## 8. Related documents

- [Adapter SDK](adapter-sdk.md)
- [Workflow state machine](workflow-state-machine.md)
- `docs/decisions/` — the reasoning behind each boundary above
- `docs/development/getting-started.md` — how to run and verify all of it
