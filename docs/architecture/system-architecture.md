# DeskWeave System Architecture

Status: Phase 1 (first real workflow: reference images → confirmation → Blender).
Audience: contributors and anyone integrating an agent with DeskWeave.

## 1. Goals

DeskWeave is **local-first agent desktop infrastructure**. It connects AI agents,
desktop applications, workflows and the user in one place, on the user's machine,
without a hosted control plane.

| Goal | How it is satisfied |
| --- | --- |
| A stable contract for desktop connectors | `deskweave-adapter-sdk` with a single `execute()` template |
| One runtime that owns lifecycle and state | `deskweave-runtime` (`Runtime`, SQLite, event bus) |
| A standard way for agents to connect | MCP server over stdio (`deskweave-mcp-gateway`) |
| A UI that shows real, not simulated, state | Tauri 2 + Vue 3 shell reading the control API |
| Safety by default | loopback-only API, token auth, permission layer, artifact store, confirmation boundary |
| A real loop, not a demo | `collect_reference_images` → user confirms → `create_reference_plane` (see [reference workflow](reference-workflow.md)) |

Still out of scope: a general DAG engine, a plugin marketplace, remote multi-user
operation, non-image artifacts, scene composition beyond reference planes.

## 2. Components

```
┌──────────────────────────────── desktop process ────────────────────────────────┐
│  Tauri 2 shell (Rust)                                                           │
│    • window / system integration                                                │
│    • spawns and supervises the Python runtime child process                     │
│    • holds the control-API token; exposes `runtime_request` / `artifact_preview`│
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
│            ├── PermissionManager        (grants, one-shot allowances, deny)     │
│            ├── WorkflowEngine           (runs, tasks, legal transitions)        │
│            ├── ArtifactStore            (ids + roots + ImageDownloader)         │
│            ├── ReferenceService         (collect → wait → confirm → execute)     │
│            ├── EventBus                 (in-process pub/sub, bounded history)   │
│            └── Database                 (aiosqlite + versioned migrations)      │
│                                                                                 │
│  Control API (service/)  ── stdlib ThreadingHTTPServer on a background thread   │
└──────────────┬──────────────────────────────────────────┬───────────────────────┘
               │ in-process adapters                      │ subprocess
               ▼                                          ▼
   ┌───────────────────────┐                  ┌──────────────────────────────┐
   │ BrowserAdapter        │                  │ BlenderAdapter               │
   │  Playwright → Chromium│                  │  blender --background …      │
   │  (headless subprocess)│                  │  fixed script + JSON config  │
   └───────────────────────┘                  └──────────────────────────────┘
               ▲
               │ MCP gateway (packages/mcp-gateway) over stdio ◄── Codex / agents
```

Adapters are in-process Python objects. Two of them start child processes of their
own (Chromium, Blender); the runtime owns neither, but each adapter closes its
child in `disconnect()`. Splitting an adapter into its own process is still
allowed later (see `docs/decisions/0004-local-process-communication.md`).

## 3. Dependency direction

```
deskweave-contracts          (pure data models + error taxonomy; depends on nothing)
      ▲        ▲
      │        │
plugin-sdk   adapter-sdk ──────► contracts
      ▲        ▲
      │        │
      │     adapters/{browser,wikimedia,blender,filesystem}
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
2. `runtime` never imports `mcp-gateway`, adapters or plugins — the composition
   root (`deskweave_runtime/bootstrap.py`) is the only place that names concrete
   adapters, and it is imported by the service/gateway entry points, not by the
   core.
3. `mcp-gateway` never imports adapters; it only calls `Runtime`.
4. Adapters never import workflow/business logic and never touch the database or
   the gateway internals. They receive a `ResourceResolver` for artifacts instead
   of a path.
5. Plugins never write core tables; they act through `PluginContext`.
6. The webview never talks to Python directly — only through the Rust bridge.

## 4. Data flow (reference-image scenario, implemented)

```
Agent (Codex)
  │ MCP: collect_reference_images(query, limit, source)
  ▼
MCP gateway ──► ReferenceService.start_collection   → task_id immediately
                     │
                     ├─ BrowserAdapter.execute → Chromium → result cards
                     │     (validation · permission · timeout · audit record)
                     ├─ ImageDownloader.import_image × n   (SSRF guard, 8 MiB, sniff)
                     ├─ ArtifactStore rows + files under <data>/artifacts
                     └─ task → waiting_for_confirmation (candidates, failures)
  ▼
Desktop UI: previews by artifact id, user selects images
  │ POST /tasks/{id}/confirm {artifact_ids, plane_name}
  ▼
ReferenceService.confirm
  ├─ task must be waiting, artifacts must belong to it
  ├─ PermissionManager.allow_once("blender", "create_reference_plane")
  └─ BlenderAdapter.execute
        ├─ blender --background --factory-startup --python create_reference_plane.py
        ├─ blender --background --python verify_scene.py   (independent check)
        └─ .blend registered as a `model` artifact
  ▼
task → succeeded | failed, execution summary stored in task.result
```

Full detail, including the trust boundary and error mapping:
[`reference-workflow.md`](reference-workflow.md).

## 5. Process boundaries

| Process | Role | Lifecycle |
| --- | --- | --- |
| Desktop shell (Rust) | window, system integration, child-process supervisor | started by the user |
| Python runtime | core state, control API, MCP server | spawned by the shell (or manually in dev) |
| Chromium | driven by the browser connector | lazily launched, closed on disconnect |
| Blender | driven per confirmation | one process per script invocation, short-lived |

There is no message broker, no Redis, no Kubernetes. One machine, at most a
handful of processes.

## 6. Agent ↔ application relationship

Agents see **tools**, not Python objects. The MCP gateway maps a tool call to a
runtime method; the runtime applies validation and permissions. An agent cannot
name an adapter method that was not declared as a capability, and cannot pass a
path that escapes the allowed roots.

Phase 1 tools: four read-only tools plus three task tools
(`collect_reference_images`, `get_reference_task`, `cancel_reference_task`).
There is deliberately **no confirmation tool**: the human decision happens in the
desktop UI, and `confirmed=true` in tool arguments is rejected as an unknown field.

## 7. Scope by phase

Implemented in Phase 0:

- workspace layout, packaging and typing for the Python distributions;
- runtime create/start/close, adapter registry, health report;
- Adapter SDK with schema validation, timeout, structured errors;
- permission model + grant persistence, default deny for non-read-only;
- SQLite schema with migrations (task, run, artifact, installation, grant, execution);
- in-process event bus with bounded history;
- MCP server over stdio, protocol-tested;
- Tauri 2 + Vue 3 desktop shell showing real health/adapters/tasks;
- CI for Python lint+test and frontend typecheck+build+test.

Implemented in Phase 1:

- real Playwright collection from a live image-search page;
- artifact store with download guards, provenance and task linkage;
- `waiting_for_confirmation` as a real, persisted, UI-visible state;
- control API for tasks/confirm/cancel + binary artifact previews;
- confirmation UI with selection, cancel, retry and reload-safe state;
- real headless Blender execution with independent scene verification;
- MCP task tools + end-to-end test of the whole chain;
- loopback-only CORS for the browser-based UI transport.

Not done: packaging/installers, code signing, OS sandboxing, non-image artifacts,
plugin-provided capabilities beyond registration, screenshots, WebSocket push.

## 8. Related documents

- [Adapter SDK](adapter-sdk.md)
- [Workflow state machine](workflow-state-machine.md)
- [Reference workflow](reference-workflow.md)
- [Browser connector](../development/browser-connector.md) ·
  [Blender connector](../development/blender-connector.md)
- `docs/decisions/` — the reasoning behind each boundary above
- `docs/development/getting-started.md` — how to run and verify all of it
