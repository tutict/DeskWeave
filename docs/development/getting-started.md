# Getting Started

Everything below was executed on Windows 11 with Python 3.12, Node 22 and
Rust 1.99 while building Phase 0. Commands that were *not* verified are marked
**NOT RUN**.

## 1. Prerequisites

| Tool | Version used | Required for |
| --- | --- | --- |
| Python | ≥ 3.12 | core runtime, tests |
| uv | 0.11.8 | Python workspace, lockfile |
| Node.js | 22.x | desktop renderer |
| pnpm | 11.25.0 (`corepack pnpm`) | JS workspace |
| Rust | 1.99 (MSVC toolchain) + WebView2 | Tauri shell only |

## 2. Python setup

```bash
uv sync --all-packages          # creates .venv and installs every workspace member
```

If `uv` is unavailable, a plain venv works as long as the five packages are
installed editable:

```bash
python -m venv .venv
.venv/Scripts/pip install -e packages/contracts -e packages/adapter-sdk \
  -e packages/plugin-sdk -e runtime -e packages/mcp-gateway
```

Checks and tests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest                    # 79 tests as of Phase 0
```

Tests write only into `.tmp-tests/` inside the repository (see `tests/conftest.py`)
and clean up after themselves.

## 3. Frontend setup

```bash
corepack enable                  # one-time: puts pnpm 11.25.0 on PATH
corepack pnpm install
corepack pnpm build              # vue-tsc --noEmit && vite build
corepack pnpm test               # vitest
corepack pnpm lint               # eslint (flat config)
corepack pnpm format:check       # prettier
corepack pnpm dev                # vite on http://localhost:1420
```

Running `pnpm dev` alone starts the UI **without** a backend: the Overview screen
shows an explicit "Runtime unavailable" error. That is the intended behaviour, not
a bug. To see live data, start the runtime first (§4).

## 4. Running the local runtime

```bash
# Foreground, announces its port on stdout and writes <data_dir>/runtime.json
uv run python -m deskweave_runtime.service --announce --port 8787

# In another shell (token is in .deskweave/runtime.json)
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8787/health
```

Environment variables (see `.env.example`): `DESKWEAVE_HOST`, `DESKWEAVE_PORT`,
`DESKWEAVE_DATA_DIR`, `DESKWEAVE_LOG_LEVEL`, `DESKWEAVE_TOKEN`.

`DESKWEAVE_HOST` must be a loopback address; anything else fails validation on
purpose.

## 5. Desktop app

```bash
cd apps/desktop
corepack pnpm tauri dev          # builds Rust + starts Vite
corepack pnpm tauri build        # installers (NOT verified in Phase 0)
```

The Rust shell spawns the Python runtime itself, using `DESKWEAVE_PYTHON` if set,
otherwise `.venv/Scripts/python.exe` (or `.venv/bin/python`) found by walking up
from the current directory, otherwise `python3`/`python` on `PATH`. Startup
failures are shown in the UI as a real error.

Data directory for the desktop app: `%APPDATA%\app.deskweave.desktop` (Windows),
`~/Library/Application Support/app.deskweave.desktop` (macOS),
`~/.local/share/app.deskweave.desktop` (Linux).

## 6. Connecting Codex (MCP)

Codex reads MCP servers from `~/.codex/config.toml`:

```toml
[mcp_servers.deskweave]
command = "C:/absolute/path/to/DeskWeave/.venv/Scripts/python.exe"   # or .../.venv/bin/python
args = ["-m", "deskweave_mcp_gateway"]
env = { DESKWEAVE_DATA_DIR = "C:/Users/you/AppData/Roaming/deskweave-mcp" }
startup_timeout_sec = 30
tool_timeout_sec = 60
```

Notes:

- Use absolute paths. Codex does not inherit the shell's working directory.
- Point `DESKWEAVE_DATA_DIR` at a **separate** directory if you want the agent's
  view (tasks, grants) isolated from the desktop app's.
- stdout is protocol-only; all diagnostics go to stderr. If you see JSON parse
  errors in Codex, check that nothing else writes to stdout.
- Verify the wiring without Codex:

```bash
uv run python -m deskweave_mcp_gateway   # then send an MCP initialize frame
```

or run the protocol tests: `uv run pytest tests/integration/test_mcp_stdio.py -v`.

## 7. CI

`.github/workflows/ci.yml` runs two jobs on Ubuntu:

| Job | Commands |
| --- | --- |
| python | `uv sync --all-packages --frozen`, `ruff check`, `ruff format --check`, `pytest` |
| frontend | `pnpm install --frozen-lockfile`, `pnpm build`, `pnpm test`, `pnpm lint`, `pnpm format:check` |

The Rust shell is not compiled in CI yet (a Tauri build needs platform system
libraries and takes several minutes). Adding a `cargo check` job is a Phase 1 item.

## 8. Repository layout

```
apps/desktop            Tauri 2 shell + Vue 3 renderer
runtime                 Python core (lifecycle, workflow, permissions, artifacts, events, persistence, service)
packages/contracts      shared pydantic models + error taxonomy
packages/adapter-sdk    connector contract, registry, mock adapter
packages/mcp-gateway    MCP server over stdio
packages/plugin-sdk     plugin contract
adapters/*              browser, blender, filesystem connectors
plugins/anime-modeling  reference workflow plugin (Phase 1 logic)
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Runtime unavailable` in the UI | Python runtime not started, or token mismatch. Check `/health` with `curl`. |
| `no Python interpreter found` in the desktop app | Set `DESKWEAVE_PYTHON` or create `.venv`. |
| `invalid runtime configuration: host ... is not a loopback address` | `DESKWEAVE_HOST` must be `127.0.0.1`, `::1` or `localhost`. |
| MCP client cannot parse responses | Something wrote to stdout; check for stray `print()` calls. |
| `ERR_PNPM_IGNORED_BUILDS: esbuild` | `allowBuilds.esbuild: true` is already set in `pnpm-workspace.yaml`; re-run `pnpm install`. |
