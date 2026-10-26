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
| Rust `wasm32-wasip1` target | `rustup target add wasm32-wasip1` | *rebuilding* the example WASM plugin only |

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
uv run pytest                    # 716 tests as of Phase 2.6.9e: 700 pass, 16 skipped.
                                 # The MCP stdio test used to be timing-flaky; it now waits
                                 # on the gateway's readiness line — see §8.
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

The interface ships in `zh-CN` and `en-US`; the picker is in **Settings →
Language** and the choice is remembered across restarts. Adding a language, the
message-key schema, error-code mapping and the CSP constraint are documented in
[i18n.md](i18n.md).

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
| WASM plugin sandbox | `uv run pytest tests/unit/test_wasm_*.py tests/contract/test_wasm_plugin_contract.py tests/integration/test_wasm_*.py` | nothing external |
| That the WASM guards are load-bearing | `uv run python scripts/wasm_guard_mutation_check.py` | **~26 min** (measured: 25 m 31 s for 61 mutations on this machine — it is 61 separate pytest invocations, not one); **mutates source files and restores them**, so do not edit the repo while it runs |

The opt-in suites are skipped by default so CI never depends on a third-party
page, on network reachability, or on a locally installed Blender. The skip message
says which environment variable enables them.

`scripts/ui_smoke.py` is the only check that exercises the UI's real transport: it
starts the runtime, builds the bundle with that URL/token, serves it, drives
Chromium through search → previews → select → confirm, asserts the candidates are
still visible after the success and survive a reload, and writes screenshots to
`.tmp-tests/` in the repository (deliberately — they are meant to be looked at,
so they do not belong in a temp directory that gets wiped).

## 8. WASM plugins (prototype)

`packages/plugin-wasm` can load and run a third-party plugin compiled to
`wasm32-wasip1` under fuel, wall-clock and memory limits, with every host call
going through the existing `PermissionManager`. Host calls are additionally capped
per invocation (`limits.max_host_calls`, counted per attempt). It is a
**library**: nothing in `Runtime`, the MCP gateway or the adapters calls it yet,
so it changes no behaviour of the running app.

Host calls are **executed**, not merely authorised: a capability runs in a bounded
pool (or on the loop, if it is a coroutine), is charged against a cost budget by
declared weight, and is admitted only if the host has room. `timed_out`,
`cancelled` and `cancellation_requested` are three different states, and only the
second means the work stopped.

Phase 2.5 added two things that are opt-in rather than always-on. **Integrity**: a
manifest may pin `wasm_sha256`, and `deskweave.install.json` may pin the version,
path, size and declared capabilities; a host built with `require_integrity=True`
or `require_install_record=True` refuses a module that drifted. This detects a
changed file — it does **not** identify a publisher. **Fairness**: a full host now
queues briefly (16 waiters, 0.25 s) and serves plugins round-robin instead of
refusing first-come, and capabilities may declare `rate_limit_per_s`. That is
bounded waiting, not strict fairness.

Phase 2.6 added the part Phase 2.5 could not answer: **who decided this plugin may
ask for these capabilities?** Three sets are now kept apart — `requested` (the
manifest's claim), `approved` (a person's decision, bound to one build) and
`granted` (`PermissionManager` rows). Installing, hashing and ABI verification
create no grants; only `approve()` does, and it reconciles the granted set rather
than adding to it, so narrowing an approval takes the dropped capability away.
Dispatch additionally requires that an `ALLOW` came from a grant or a one-shot
confirmation **and** that a current approval covers that capability for that
build, so neither a manifest declaring a read-only capability nor a hand-written
grant row can authorise a call. Revocation reports `queued_stopped` / `running` /
`still_running` separately and claims no kill. Details:
[plugin-lifecycle.md](../architecture/plugin-lifecycle.md).

Phase 2.3 through 2.6.9b are the reason to trust it: 130 of its 398 tests live in
the seven adversarial suites, and `scripts/wasm_guard_mutation_check.py` deletes
each control in turn to prove those tests are load-bearing. Read
[the trust boundary map](../architecture/wasm-trust-boundary.md) before changing
anything in this package — it says which layer is supposed to enforce what, and
therefore which layer a bug belongs to.

```bash
# run the sandbox and lifecycle tests (no Rust toolchain needed: the .wasm is committed)
# 398 tests: 212 sandbox (82 behavioural/contract, 130 adversarial)
#            + 186 lifecycle, governance and authorisation
uv run pytest tests/unit/test_wasm_abi.py tests/unit/test_wasm_manifest.py \
  tests/unit/test_wasm_integrity.py \
  tests/contract/test_wasm_plugin_contract.py \
  tests/integration/test_wasm_plugin_runtime.py \
  tests/integration/test_wasm_host_capability.py \
  tests/integration/test_wasm_plugin_negative.py \
  tests/integration/test_wasm_authorisation_adversarial.py \
  tests/integration/test_wasm_host_budget.py \
  tests/integration/test_wasm_invariants.py \
  tests/integration/test_wasm_host_execution.py \
  tests/integration/test_wasm_install_integrity.py \
  tests/integration/test_wasm_fair_scheduling.py \
  tests/unit/test_plugin_lifecycle.py \
  tests/integration/test_plugin_lifecycle_runtime.py \
  tests/integration/test_plugin_authorisation_boundary.py \
  tests/integration/test_plugin_approval_consistency.py \
  tests/integration/test_lifecycle_integrity.py \
  tests/integration/test_governance_identity.py \
  tests/integration/test_activation_snapshot.py \
  tests/integration/test_store_lock.py \
  tests/integration/test_withdrawal_ownership.py \
  tests/integration/test_lifecycle_closure.py \
  tests/integration/test_uninstall_failure.py \
  tests/integration/test_lifecycle_construction.py \
  tests/integration/test_lock_errors.py \
  tests/integration/test_close_race.py \
  tests/integration/test_pin_identity.py \
  tests/integration/test_revoke_recovery.py \
  tests/integration/test_aclose_reliability.py \
  tests/unit/test_plugin_admin_cli.py -q

# prove the guards are load-bearing: delete each control, require a red test
# NB: this temporarily rewrites source files and restores them byte-for-byte
# (SHA-256 checked). Do not edit the repository while it runs.
uv run python scripts/wasm_guard_mutation_check.py    # expect 61/61 caught, 0 survived, 0 skipped

# read-only inspection of a plugin directory (no verb can grant anything)
uv run python scripts/plugin_admin.py list examples/wasm-plugins
uv run python scripts/plugin_admin.py inspect reference-metadata \
  --plugins-dir examples/wasm-plugins

# rebuild the example plugin (needs `rustup target add wasm32-wasip1`)
uv run python scripts/build_wasm_plugin.py
```

The committed artifact is `examples/wasm-plugins/reference-metadata/reference_metadata.wasm`
(99,279 bytes, sha256 `b730b7df9408189c16cd360804bde33a28ad69060a049c6cc55c9539fe9e94c8`).
CI does not build it — it is committed precisely so the Python tests stay hermetic
and the CI runner needs no Rust target.

**One formerly timing-flaky test outside this package.**
`tests/integration/test_mcp_stdio.py::test_stdout_is_not_polluted_by_logs` started
the MCP gateway as a subprocess and waited a fixed 2.5 s for it to log
`MCP gateway ready`. Measured on this machine, the gateway reaches that line 2.33–2.46 s
after start (five samples; startup includes Blender discovery), so the test sat on a
~100 ms margin and failed intermittently under load — 1 pass and 3 failures across four
runs of the *same* command with no code change. The Phase 2.6.3 review replaced the
sleep with a deterministic wait: stdout and stderr are pumped on reader threads, the
test blocks on a `threading.Event` set by the readiness line (30 s ceiling, so a real
hang still fails), then closes stdin and waits for the process to exit. The assertion
is unchanged — `stdout == b""`, so a log written to the protocol channel still fails.
Measured afterwards: 5/5 passes at ~2.85 s. The guard itself was verified by injecting
a `print()` into `packages/mcp-gateway/src/deskweave_mcp_gateway/__main__.py` and
watching the test go red, then restoring the file from a backup and confirming the
SHA-256 was unchanged.

- Design and measurements: [ADR 0014](../decisions/0014-wasm-plugin-isolation.md)
- How host calls are executed, priced and cancelled: [host-capability-execution.md](../architecture/host-capability-execution.md)
- Why the executor looks like that: [ADR 0015](../decisions/0015-host-capability-execution.md)
- Install records, hashes and path rules: [plugin-installation.md](../architecture/plugin-installation.md)
- Why content pinning instead of signatures, and why no scheduler: [ADR 0016](../decisions/0016-plugin-provenance-and-fairness.md)
- Approval, activation, revocation and the three permission sets: [plugin-lifecycle.md](../architecture/plugin-lifecycle.md)
- Why provenance went into the contract instead of the plugin host: [ADR 0017](../decisions/0017-plugin-approval-boundary.md)
- Layer-by-layer trust boundary: [wasm-trust-boundary.md](../architecture/wasm-trust-boundary.md)
- Call chain and ABI: [wasm-plugin-runtime.md](../architecture/wasm-plugin-runtime.md)
- What it does and does not protect against: [wasm-threat-model.md](../architecture/wasm-threat-model.md)
- What a plugin may call: [wasm-capability-catalogue.md](../architecture/wasm-capability-catalogue.md)
- Building one: [writing-a-wasm-plugin.md](writing-a-wasm-plugin.md)

## 9. Connecting Codex (MCP)

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

## 10. CI

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

**Verified on GitHub** as of Phase 2.1: four runs on `feat/phase-2-baseline`, the
last one green, on `ubuntu-26.04` with `pnpm/action-setup@v6`. The first two runs
failed and their fixes are recorded in the commits; the workflow is no longer
"parses but unexecuted".

## 11. Repository layout

```
apps/desktop            Tauri 2 shell + Vue 3 renderer
runtime                 Python core (lifecycle, workflow, permissions, artifacts, events, persistence, service)
packages/contracts      shared pydantic models + error taxonomy
packages/adapter-sdk    connector contract, registry, mock adapter
packages/mcp-gateway    MCP server over stdio
packages/plugin-sdk     plugin contract
packages/plugin-wasm    WASM plugin sandbox (host, ABI, capability registry)
adapters/*              browser, blender, filesystem connectors
plugins/anime-modeling  plugin that registers connectors
examples/wasm-plugins   example WASM plugin (Rust) + its committed .wasm
scripts/ui_smoke.py     end-to-end UI check against a real runtime
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## 12. Troubleshooting

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
| `plugin_malformed` — "memory minimum size … exceeds memory limits" | The WASM module declares a minimum linear memory larger than the manifest's `max_memory_bytes`. |
| `plugin_capability_denied` on a WASM plugin call | The plugin declared the capability but no grant covers `wasm-plugin:<plugin_id>`. A grant to an adapter id does not transfer. |
| `plugin_resource_exhausted` | Fuel ran out. Measure `result.fuel_used` on a real run and raise `limits.fuel` in the manifest. |
| `plugin_resource_exhausted` with `details.reason` mentioning the budget | The plugin made more than `limits.max_host_calls` host calls in one invocation. Raise the limit (ceiling 1024) or batch the requests; a retry loop reaches this faster than anything else, because refusals are counted too. |
| `plugin_host_error` | The capability raised, or the host call timed out. This is a host-side failure, not a denial — `details` names the exception type and the elapsed time, never a token or a path. `details["state"]` says which of `failed` / `timed_out` / `cancelled` / `cancellation_requested` it was. |
| `plugin_resource_exhausted` with `decision "concurrency"` | Admission refused: too many host calls already in flight (per invocation, per plugin, host-wide, or the blocking pool is full). Nothing ran and nothing was charged. Retrying immediately cannot help — admission never queues. |
| `plugin_resource_exhausted` with `decision "cost"` | The invocation's cost budget cannot cover another call at this capability's weight. Reduce calls, or raise `limits.max_cost` (ceiling 4096). |
| A plugin invocation outlives its `timeout_ms` by the host-call deadline | The guest was inside a host call when its wall clock ran out. The capability cannot be interrupted; the invocation waits for the host-call deadline. This is the documented limit, not a hang. |
