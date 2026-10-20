# DeskWeave

**English** · [简体中文](README.zh-CN.md)

**Local-first Agent Desktop Infrastructure.**

DeskWeave connects AI agents, desktop applications, workflows and you — on your
machine, with no hosted control plane. Agents speak MCP; the core owns state,
permissions and audit; a Tauri shell gives you a real window onto both.

> Status: **Phase 2 — extensibility verified by building a second source.**
> The loop from Phase 1.5 still holds: an agent collects reference images, the
> images land in a local artifact store, *you* pick the ones to use, and only then
> does Blender build a reference plane from exactly that selection. Phase 2 asked
> whether a new capability can be added without editing the runtime core, and
> answered it by adding a second image source (`wikimedia`, public Commons API, no
> key) that needed **no change** to the workflow, the MCP gateway, the control API
> or the UI — only a `source → connector` mapping in the composition root.

## Capability matrix

| Capability | State | Evidence |
| --- | --- | --- |
| Python core runtime (lifecycle, config, health) | **implemented** | `tests/unit/test_runtime.py`, `tests/e2e/test_service_cli.py` |
| Adapter SDK (capabilities, JSON-Schema validation, timeouts, structured errors) | **implemented** | `tests/contract/test_adapter_contract.py` |
| Adapter registry + permission layer (grants, one-shot allowances, default deny) | **implemented** | `tests/unit/test_permissions.py`, `test_registry.py` |
| SQLite persistence with versioned migrations | **implemented** | `tests/unit/test_persistence.py` |
| In-process event bus | **implemented** | `tests/unit/test_events.py` |
| MCP server over stdio, 7 tools | **implemented** | `tests/integration/test_mcp_stdio.py`, `test_mcp_reference_tools.py` |
| Desktop shell (Tauri 2 + Vue 3) with confirmation UI | **implemented** | `apps/desktop/src/views/ReferencesView.test.ts`, `scripts/ui_smoke.py` |
| `filesystem` connector (read-only, allowed-root constrained) | **implemented** | `tests/integration/test_filesystem_adapter.py` |
| `browser` connector — real image search via Playwright | **implemented** | `tests/integration/test_browser_real.py` (opt-in) |
| `wikimedia` connector — public Commons API, no key, returns licence + author per image | **implemented** | `tests/integration/test_wikimedia_real.py` (opt-in), `tests/unit/test_wikimedia_api.py` |
| Reference sources: `source → connector` registry, wired at bootstrap | **implemented** — a new source needs no change to the workflow, gateway, API or UI | `tests/integration/test_reference_api.py`, `docs/decisions/0013-collection-source-registry.md` |
| Adapter developer guide (quick start, test templates, boundaries) | **implemented** | `docs/development/writing-an-adapter.md` |
| Artifact store with download guards + provenance | **implemented** | `tests/unit/test_artifact_downloader.py` |
| `blender` connector — headless plane creation + independent verification | **implemented** | `tests/integration/test_blender_real.py` (opt-in) |
| Runtime child supervision (Windows Job Object, graceful → forced stop) | **implemented (Windows)** — `supervised: false` on other platforms | `cargo test --lib job::tests`, `docs/decisions/0009-runtime-child-supervision.md` |
| Runtime token kept out of the data directory | **implemented** — per-run token in the child's environment, legacy token removed on start | `tests/integration/test_state_file.py`, `docs/decisions/0010-runtime-token-storage.md` |
| Restart recovery of interrupted tasks | **implemented** — evidence-based, never auto-executes | `tests/unit/test_recovery.py`, `tests/integration/test_recovery_service.py` |
| Produced `.blend`: metadata + "show in folder" / "open in Blender" | **implemented** — by artifact id, never by path | `tests/unit/test_artifact_opener.py`, `ReferencesView.test.ts` |
| Inline thumbnail of the produced scene | **not implemented** — the UI says so instead of showing a placeholder | `docs/decisions/0012-blender-output-observability.md` |
| Child supervision on macOS / Linux | **not implemented** — no Job Objects; graceful stop + kill of the direct child only | `docs/decisions/0009-runtime-child-supervision.md` |
| `anime-modeling` plugin workflow logic | **experimental** — registers connectors only | `plugins/anime-modeling/` |
| Screenshots / page capture | **not implemented** — no capability exposed | — |
| Non-image artifacts, scene composition beyond reference planes | **not implemented** | — |
| Installers / code signing | **not built, not verified** | — |
| Remote or multi-user operation | **not implemented by design** | `docs/decisions/0004-local-process-communication.md` |

## Quick start

```bash
# Python core + browser binary
uv sync --all-packages
uv run playwright install chromium
uv run pytest
uv run python -m deskweave_runtime.service --announce --port 8787

# Desktop UI (second shell)
corepack pnpm install
corepack pnpm build && corepack pnpm test
cd apps/desktop && corepack pnpm tauri dev
```

Blender is discovered automatically (`DESKWEAVE_BLENDER` overrides). Full
instructions, including Codex MCP configuration: **[docs/development/getting-started.md](docs/development/getting-started.md)**.

## Architecture in one picture

```
Codex / agent ──MCP/stdio──► mcp-gateway ──► Runtime ──► adapters (browser, wikimedia, blender, fs)
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
- [Reference workflow](docs/architecture/reference-workflow.md) — the real loop, step by step
- [Adapter SDK](docs/architecture/adapter-sdk.md)
- [Workflow state machine](docs/architecture/workflow-state-machine.md)
- Connectors: [browser](docs/development/browser-connector.md) ·
  [Blender](docs/development/blender-connector.md)
- Writing your own: [writing an adapter](docs/development/writing-an-adapter.md)
- Decisions: [Tauri](docs/decisions/0001-desktop-framework-tauri.md) ·
  [Python core](docs/decisions/0002-python-core-runtime.md) ·
  [MCP boundary](docs/decisions/0003-mcp-gateway-boundary.md) ·
  [Local IPC](docs/decisions/0004-local-process-communication.md) ·
  [SQLite](docs/decisions/0005-sqlite-persistence.md) ·
  [Browser source](docs/decisions/0006-browser-reference-source.md) ·
  [Download boundary](docs/decisions/0007-artifact-download-boundary.md) ·
  [Blender headless](docs/decisions/0008-blender-headless-execution.md) ·
  [Child supervision](docs/decisions/0009-runtime-child-supervision.md) ·
  [Token storage](docs/decisions/0010-runtime-token-storage.md) ·
  [Restart recovery](docs/decisions/0011-restart-recovery.md) ·
  [Blender output](docs/decisions/0012-blender-output-observability.md) ·
  [Source registry](docs/decisions/0013-collection-source-registry.md)

## Security posture

DeskWeave drives a browser and Blender, so the defaults matter:

- control API binds to **loopback only** (enforced by config validation);
- every request needs a **per-run token**; the webview never sees it, and it is
  **not written to disk** (a legacy plaintext token is removed on first start);
- the webview can call exactly two Rust commands (`runtime_request`,
  `artifact_preview`), restricted to a structural route allow-list;
- the runtime child runs inside a **Windows Job Object** with
  `KILL_ON_JOB_CLOSE`, so a shell crash cannot orphan it or a Blender run;
- tool arguments are **rejected** if undeclared, then schema-validated —
  including `confirmed=true`, which grants nothing;
- **no tool can confirm a selection**: confirmation is a UI action, verified
  server-side against the task state and artifact ownership;
- downloads are guarded against SSRF (public IPs only, per-hop re-validation),
  capped at 8 MiB, sniffed by magic bytes, and written atomically;
- file access is constrained to **allowed roots**; a path is never an
  authorization token, and adapters only ever receive artifact ids;
- Blender runs a **fixed script with a JSON config** — no Python from arguments —
  and the result is verified in a second process;
- opening a produced file is a **user action** (`reveal` / `open` on an artifact
  id); the runtime resolves and re-checks the path, the webview never sees one;
- there is **no generic code-execution capability** — the permission layer denies
  `code:execution` unconditionally;
- execution records and permission grants are persisted for audit.

Known gaps: there is no OS sandbox around the adapters; a same-user process can
read the child's environment (that is the trust boundary); on macOS/Linux a hard
kill of the shell can still leave the runtime running (no Job Objects there, see
`docs/decisions/0009-runtime-child-supervision.md`).

## Repository layout

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript shell
runtime/                Python core runtime and control API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk}
adapters/{browser,blender,filesystem}
plugins/anime-modeling
scripts/ui_smoke.py
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## Commands

| Task | Command |
| --- | --- |
| Python lint | `uv run ruff check .` |
| Python format check | `uv run ruff format --check .` |
| Python tests | `uv run pytest` |
| Real browser tests | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py` |
| Real Blender tests | `DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py` |
| Full chain | `DESKWEAVE_NETWORK_TESTS=1 DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/e2e` |
| Frontend build | `corepack pnpm build` |
| CI, locally (same steps as GitHub Actions) | `uv run python scripts/ci_local.py` |
| Frontend tests | `corepack pnpm test` |
| Frontend lint / format | `corepack pnpm lint` / `corepack pnpm format:check` |
| Rust bridge | `cargo test --lib` (in `apps/desktop/src-tauri`) |
| MCP server | `uv run python -m deskweave_mcp_gateway` |
| Real UI smoke | `uv run python scripts/ui_smoke.py` |

## License

MIT — see [LICENSE](LICENSE).
