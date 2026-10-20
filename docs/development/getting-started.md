# Getting Started

Everything below was executed on Windows 11 with Python 3.12, Node 22,
Rust 1.99, Playwright 1.64 and Blender 5.2.0 while building Phase 1. Commands that
were *not* verified are marked **NOT RUN**.

## 1. Prerequisites

| Tool | Version used | Required for |
| --- | --- | --- |
| Python | ≥ 3.12 | core runtime, tests |
| uv | 0.11.8 | Python workspace, lockfile |
| Node.js | 22.x | desktop renderer |
| pnpm | 11.25.0 (`corepack pnpm`) | JS workspace |
| Rust | 1.99 (MSVC toolchain) + WebView2 | Tauri shell only |
| Chromium (Playwright) | 1.64 build | real image collection |
| Blender | 5.2.0 LTS (≥ 3.0) | creating reference planes |

Chromium and Blender are only needed for the *real* workflow. Without them the
runtime still starts, and the affected capability reports a connection error
instead of pretending to work.

## 2. Python setup

```bash
uv sync --all-packages                # creates .venv and installs every workspace member
uv run playwright install chromium    # browser binary for the browser connector
```

If `uv` is unavailable, a plain venv works as long as the packages are installed
editable:

```bash
python -m venv .venv
.venv/Scripts/pip install -e packages/contracts -e packages/adapter-sdk \
  -e packages/plugin-sdk -e runtime -e packages/mcp-gateway \
  -e adapters/browser -e adapters/blender -e adapters/filesystem
```

Checks and tests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest                    # 214 passed, 13 skipped as of Phase 1.5
```

Tests write only into a per-run directory under the OS temp directory
(`%TEMP%/deskweave-tests/run-<pid>-<id>`; see `tests/conftest.py`) and remove it
when the session ends. Set `DESKWEAVE_TEST_TMP` to relocate it to another
disposable directory — never point it inside the repository, because a full run
creates and deletes thousands of files.

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
a bug. To see live data, start the runtime first (§4) and point the UI at it with
`VITE_DESKWEAVE_RUNTIME_URL` / `VITE_DESKWEAVE_RUNTIME_TOKEN` — or use the Tauri
shell, which starts the runtime itself and never exposes the token.

## 4. Running the local runtime

```bash
# Foreground, announces its port on stdout and writes <data_dir>/runtime.json
uv run python -m deskweave_runtime.service --announce --port 8787

# In another shell (token is in .deskweave/runtime.json)
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8787/health
```

Environment variables (see `.env.example`): `DESKWEAVE_HOST`, `DESKWEAVE_PORT`,
`DESKWEAVE_DATA_DIR`, `DESKWEAVE_LOG_LEVEL`, `DESKWEAVE_TOKEN`, plus
`DESKWEAVE_BLENDER` for the Blender executable.

`DESKWEAVE_HOST` must be a loopback address; anything else fails validation on
purpose.

## 5. The real workflow (collect → confirm → Blender)

This is the Phase 1 loop; the architecture is described in
[`docs/architecture/reference-workflow.md`](../architecture/reference-workflow.md).

```bash
# 1. start a collection (returns immediately)
curl -s -X POST http://127.0.0.1:8787/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"query": "anime character reference sheet", "limit": 6}'

# 1b. the same call against a different source (default is "bing")
curl -s -X POST http://127.0.0.1:8787/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"query": "anime character reference sheet", "limit": 6, "source": "wikimedia"}'

# 2. poll until status == waiting_for_confirmation
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8787/tasks/<task_id>

# 3. confirm specific artifact ids (this is what runs Blender)
curl -s -X POST http://127.0.0.1:8787/tasks/<task_id>/confirm \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"artifact_ids": ["art_…"], "plane_name": "CharacterRef"}'
```

`source` is optional and defaults to `bing`. `wikimedia` needs no key, no login and
no extra install beyond `uv sync --all-packages`: it calls the public Commons API
and returns a licence and an author with each image. An unknown source is a `400`
(`validation_error`); a known source with no connector installed is a `503`
(`adapter_connection_error` with `details.source`) — neither creates a task.

The desktop UI does exactly these calls (through the Rust bridge) and adds the
selection screen, previews, cancel and reload-safe state.
Connector-specific setup and failure modes:
[browser](../development/browser-connector.md) ·
[Blender](../development/blender-connector.md).
Building your own connector: [writing-an-adapter.md](writing-an-adapter.md).

## 6. Desktop app

```bash
cd apps/desktop
corepack pnpm tauri dev          # builds Rust + starts Vite (+ the Python runtime)
corepack pnpm tauri build        # installers (NOT RUN in Phase 0/1)
```

The Rust shell spawns the Python runtime itself, using `DESKWEAVE_PYTHON` if set,
otherwise `.venv/Scripts/python.exe` (or `.venv/bin/python`) found by walking up
from the current directory, otherwise `python3`/`python` on `PATH`. Startup
failures are shown in the UI as a real error.

Data directory for the desktop app: `%APPDATA%\app.deskweave.desktop` (Windows),
`~/Library/Application Support/app.deskweave.desktop` (macOS),
`~/.local/share/app.deskweave.desktop` (Linux). Artifacts live under
`<data_dir>/artifacts/`.

## 7. Tests

| Suite | Command | Needs |
| --- | --- | --- |
| Python unit + integration | `uv run pytest` | nothing external |
| Real browser | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py -v` | network + Chromium |
| Real Wikimedia Commons | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_wikimedia_real.py -v` | network only |
| Real Blender | `DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py -v` | Blender installed |
| Full chain (MCP → web → Blender) | `DESKWEAVE_NETWORK_TESTS=1 DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/e2e -v` | both |
| Frontend | `corepack pnpm test` | Node |
| Rust bridge | `cargo test --lib` in `apps/desktop/src-tauri` | Rust toolchain |
| Real UI against a real runtime | `uv run python scripts/ui_smoke.py` | Chromium, Blender |

The opt-in suites are skipped by default so CI never depends on a third-party
page, on network reachability, or on a locally installed Blender. The skip message
says which environment variable enables them.

`scripts/ui_smoke.py` is the only check that exercises the UI's real transport: it
starts the runtime, builds the bundle with that URL/token, serves it, drives
Chromium through search → previews → select → confirm, asserts the candidates are
still visible after the success and survive a reload, and writes screenshots to
`.tmp-tests/` in the repository (deliberately — they are meant to be looked at,
so they do not belong in a temp directory that gets wiped).

## 8. Connecting Codex (MCP)

Codex reads MCP servers from `~/.codex/config.toml`:

```toml
[mcp_servers.deskweave]
command = "C:/absolute/path/to/DeskWeave/.venv/Scripts/python.exe"   # or .../.venv/bin/python
args = ["-m", "deskweave_mcp_gateway"]
env = { DESKWEAVE_DATA_DIR = "C:/Users/you/AppData/Roaming/deskweave-mcp", DESKWEAVE_BLENDER = "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" }
startup_timeout_sec = 30
tool_timeout_sec = 60
```

Notes:

- Use absolute paths. Codex does not inherit the shell's working directory.
- Point `DESKWEAVE_DATA_DIR` at a **separate** directory if you want the agent's
  view (tasks, grants, artifacts) isolated from the desktop app's.
- The agent can start and inspect a collection, but it **cannot confirm one**:
  there is no confirmation tool. The user confirms in the desktop UI.
- stdout is protocol-only; all diagnostics go to stderr. If you see JSON parse
  errors in Codex, check that nothing else writes to stdout.
- Verify the wiring without Codex:

```bash
uv run python -m deskweave_mcp_gateway   # then send an MCP initialize frame
```

or run the protocol tests: `uv run pytest tests/integration/test_mcp_stdio.py -v`.

## 9. CI

`.github/workflows/ci.yml` runs three jobs on Ubuntu:

| Job | Commands |
| --- | --- |
| python | `uv sync --all-packages --locked`, `ruff check`, `ruff format --check`, `pytest` |
| frontend | `pnpm install --frozen-lockfile`, `pnpm build`, `pnpm test`, `pnpm lint`, `pnpm format:check` |
| rust | Tauri system deps, `cargo check --all-targets --locked`, `cargo fmt --check`, `cargo test --lib --locked` |

The opt-in network/Blender suites do **not** run in CI (no Blender on the runner,
and a third-party page is not a dependency worth flaking on). Run them locally
before a release.

`scripts/ci_local.py` runs exactly those commands on the development machine — it
is the only way to know the workflow's commands still work without a run on
GitHub:

```bash
uv run python scripts/ci_local.py            # all three jobs
uv run python scripts/ci_local.py python     # one job
uv run python scripts/ci_local.py --list     # what would run, and where
```

On Windows `pnpm` resolves to a `.CMD` shim, which `CreateProcess` cannot start
directly; the runner therefore spawns it through `cmd /c`. Without that it reports
"pnpm is not installed" on a machine where pnpm works fine in a shell.

**NOT RUN**: the workflow file was never executed on GitHub from this machine. The
three command sets *were* run locally (§2, §3, §7) and pass, and the YAML parses,
but the CI job itself is unverified.

## 10. Repository layout

```
apps/desktop            Tauri 2 shell + Vue 3 renderer
runtime                 Python core (lifecycle, workflow, permissions, artifacts, events, persistence, service)
packages/contracts      shared pydantic models + error taxonomy
packages/adapter-sdk    connector contract, registry, mock adapter
packages/mcp-gateway    MCP server over stdio
packages/plugin-sdk     plugin contract
adapters/*              browser, blender, filesystem connectors
plugins/anime-modeling  plugin that registers connectors
scripts/ui_smoke.py     end-to-end UI check against a real runtime
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Runtime unavailable` in the UI | Python runtime not started, or token mismatch. Check `/health` with `curl`. |
| `no Python interpreter found` in the desktop app | Set `DESKWEAVE_PYTHON` or create `.venv`. |
| `invalid runtime configuration: host ... is not a loopback address` | `DESKWEAVE_HOST` must be `127.0.0.1`, `::1` or `localhost`. |
| MCP client cannot parse responses | Something wrote to stdout; check for stray `print()` calls. |
| `ERR_PNPM_IGNORED_BUILDS: esbuild` | `allowBuilds.esbuild: true` is already set in `pnpm-workspace.yaml`; re-run `pnpm install`. |
| `401 unauthorized` from a browser tab | The control API only sends CORS headers to loopback origins, and the token must be sent as `Authorization: Bearer …`. |
| Task stuck in `running` after a crash | State is persisted, not resumed: start a new collection. The stale task stays visible for audit. |
| `browser_access_denied` | The search provider asked for human verification. Resolve it in a normal browser and retry; the connector will not solve it. |
| `no Blender executable found …` | Install Blender ≥ 3.0 or set `DESKWEAVE_BLENDER`. `details.checked` lists the tried paths. |
| `cargo` cannot find WebKitGTK on Linux | Install `libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf` (same list as the CI rust job). |
