# DeskWeave

**English** · [简体中文](README.zh-CN.md)

**Local-first Agent Desktop Infrastructure.**

DeskWeave connects AI agents, desktop applications, workflows and you — on your
machine, with no hosted control plane. Agents speak MCP; the core owns state,
permissions and audit; a Tauri shell gives you a real window onto both.

> Status: **Phase 0 — engineering foundation.** The skeleton runs, is tested, and
> is safe by default. Real browser automation and Blender integration are Phase 1.

## What exists today

| Capability | State |
| --- | --- |
| Python core runtime (lifecycle, config, health) | implemented |
| Adapter SDK (capabilities, JSON-Schema validation, timeouts, structured errors) | implemented |
| Adapter registry + permission layer (default deny for non-read-only) | implemented |
| SQLite persistence with versioned migrations (6 entities) | implemented |
| In-process event bus | implemented |
| MCP server over stdio with 4 read-only tools | implemented, protocol-tested |
| Desktop shell (Tauri 2 + Vue 3) showing real runtime state | implemented |
| `filesystem` adapter (read-only, allowed-root constrained) | implemented |
| `browser` / `blender` adapters | interfaces + lifecycle only, execution raises `NotImplementedYetError` |
| Plugin `anime-modeling` | registers connectors; workflow logic is Phase 1 |
| Installers / code signing | not built, not verified |

## Quick start

```bash
# Python core
uv sync --all-packages
uv run pytest
uv run python -m deskweave_runtime.service --announce --port 8787

# Desktop UI (second shell)
corepack pnpm install
corepack pnpm build && corepack pnpm test
cd apps/desktop && corepack pnpm tauri dev
```

Full instructions, including Codex MCP configuration: **[docs/development/getting-started.md](docs/development/getting-started.md)**.

## Architecture in one picture

```
Codex / agent ──MCP/stdio──► mcp-gateway ──► Runtime ──► adapters (browser, blender, fs)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                   permission layer      workflow engine        SQLite
                        │                     │
                        └──────── in-process event bus ──────────┘
                                              ▲
                        Tauri shell ──HTTP──► │  (loopback + token, Rust-mediated)
                        Vue 3 UI ─────────────┘
```

Dependency direction is enforced by package layout: `contracts` ← `adapter-sdk` ←
`runtime` ← `mcp-gateway`. Adapters never import workflow logic; the UI never
imports Python.

Read more:

- [System architecture](docs/architecture/system-architecture.md)
- [Adapter SDK](docs/architecture/adapter-sdk.md)
- [Workflow state machine](docs/architecture/workflow-state-machine.md)
- Decisions: [Tauri](docs/decisions/0001-desktop-framework-tauri.md) ·
  [Python core](docs/decisions/0002-python-core-runtime.md) ·
  [MCP boundary](docs/decisions/0003-mcp-gateway-boundary.md) ·
  [Local IPC](docs/decisions/0004-local-process-communication.md) ·
  [SQLite](docs/decisions/0005-sqlite-persistence.md)

## Security posture

DeskWeave can eventually drive a browser and Blender, so the defaults matter:

- control API binds to **loopback only** (enforced by config validation);
- every request needs a **per-run token**; the webview never sees it;
- the webview can call exactly one Rust command, restricted to four read paths;
- tool arguments are **rejected** if undeclared, then schema-validated;
- file access is constrained to **allowed roots**, and a path is never an
  authorization token;
- there is **no generic code-execution capability** — the permission layer denies
  `code:execution` unconditionally;
- execution records and permission grants are persisted for audit.

Known gaps (see `docs/decisions/0004-local-process-communication.md`): the token is
stored in plaintext in the runtime data directory, there is no OS sandbox, and a
hard-killed shell may leave the Python child running.

## Repository layout

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript shell
runtime/                Python core runtime and control API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk}
adapters/{browser,blender,filesystem}
plugins/anime-modeling
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## Commands

| Task | Command |
| --- | --- |
| Python lint | `uv run ruff check .` |
| Python format check | `uv run ruff format --check .` |
| Python tests | `uv run pytest` |
| Frontend build | `corepack pnpm build` |
| Frontend tests | `corepack pnpm test` |
| Frontend lint / format | `corepack pnpm lint` / `corepack pnpm format:check` |
| MCP server | `uv run python -m deskweave_mcp_gateway` |

## License

MIT — see [LICENSE](LICENSE).
