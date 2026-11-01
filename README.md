# DeskWeave

**经纬** — the warp and the weft: one fixed core, and everything you thread through it.

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml/badge.svg)](https://github.com/tutict/DeskWeave/actions/workflows/ci.yml)

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
>
> Phase 2.2 asked the harder version of that question: can a capability come from
> code this project did not write, in a language it does not use, without handing
> it the file system, the network or a shell? A WebAssembly sandbox now runs such
> a plugin under fuel, wall-clock and memory limits, with one authorised host-call
> channel into the existing permission layer. It is a tested library, **not yet
> wired into `Runtime`** — see [`docs/decisions/0014-wasm-plugin-isolation.md`](docs/decisions/0014-wasm-plugin-isolation.md)
> and the [threat model](docs/architecture/wasm-threat-model.md) for what it does
> and does not protect.
>
> Phase 2.3 attacked that boundary instead of describing it — 129 tests at the
> time, 66 of them existing only to break it (398 and 130 today); five real
> escapes found and fixed — a plugin could make `invoke()` raise `IndexError`,
> `RecursionError` or `TimeoutError` without ever holding a grant; a
> per-invocation host-call budget added because the tests showed the old design
> had none; and mutation checks that delete each control in turn and require the
> suite to go red — sixty-one of them today, 61/61 caught, 0 skipped.
> Every claim is filed as *verified*, *depends on the host*, *not guaranteed* or
> *not verified on this platform* in the threat model, so the document cannot be
> read as stronger than the evidence.
>
> Phase 2.4 fixed what 2.3 recorded but could not fix: a host capability ran on
> the event loop, so 250 ms of capability froze every other coroutine, and
> `cancel()` stopped the caller waiting without stopping the work. Capabilities now
> run in a bounded pool, are admitted against four concurrency limits and a cost
> budget, and report one state per call — `timed_out` and `cancelled` are no longer
> the same word. A synchronous capability still cannot be interrupted, and the
> host says so in the audit (`confirmed_stopped: false`) instead of a comment.
>
> Phase 2.5 fixed the two things that were true of a *correct* plugin: it could be
> silently replaced on disk, and a well-behaved one could be starved by a busy one.
> Modules can now be pinned by content — a manifest hash, or an install record
> carrying version, path, size and declared capabilities — and a host built with
> `require_integrity` refuses a module that drifted. That detects a changed file;
> it does **not** identify a publisher, and no signing infrastructure was invented
> to pretend otherwise. Admission is no longer first-come: a full host queues 16
> waiters for at most 0.25 s and serves plugins round-robin, with optional
> per-capability rate limits. The claim is **bounded waiting, not strict
> fairness** — no weighting, no latency guarantee, per host instance. See
> [`docs/architecture/plugin-installation.md`](docs/architecture/plugin-installation.md)
> and [ADR 0016](docs/decisions/0016-plugin-provenance-and-fairness.md).
>
> Phase 2.6 asked who had decided any of that. An audit of the code found that a
> plugin could authorise **itself**: the permission layer auto-allows read-only
> risk, so a manifest declaring a read-only capability received an `ALLOW` with no
> human involved. Three sets are now kept apart — `requested` (the manifest's
> claim), `approved` (a person's decision, bound to one build) and `granted` (the
> permission rows dispatch honours). Installing, hashing and ABI verification
> create no grants; only `approve()` does, and dispatch additionally requires that
> an `ALLOW` came from a grant or a one-shot confirmation **and** that a current
> approval covers the capability — a grant alone is not an approval. Reinstalling
> clears the approval instead of inheriting it *when the build changed*, and writes
> nothing at all when it did not. Revocation deletes the grants *and* pending
> one-shots *and* refuses everything still queued; it reports `queued_stopped`,
> `running` and `stopped=False` rather than claiming a running operation was
> killed. An approval is a local record of a local decision, not a publisher
> credential. See
> [`docs/architecture/plugin-lifecycle.md`](docs/architecture/plugin-lifecycle.md)
> and [ADR 0017](docs/decisions/0017-plugin-approval-boundary.md).
>
> Phase 2.6.10 went to the half of the product the runtime never touches: the
> window. English strings were baked into templates, and — worse — error text was
> assembled from whatever the runtime client happened to throw, so by the time a
> view rendered it the code was gone and the prose could not be translated at all.
> The interface now ships in `zh-CN` and `en-US`, 131 keys against one typed
> schema (a missing key is a build error, not a fallback), and errors are
> structured and turned into words at render time — which is why a failure already
> on screen when you change language comes back in the new one. One constraint
> decided the library: the Tauri CSP has no `unsafe-eval`, so vue-i18n had to be
> v11, which interprets a message AST instead of calling `new Function`; the built
> bundle contains zero occurrences of either. The packaged shell was not exercised
> — it crashes on launch on the machine this was built on — so switching language
> *inside the real window* is recorded as unverified rather than claimed.
>
> The pipeline is green: three jobs (Python, frontend, Rust) run on every push
> to `main` and on every pull request, on Ubuntu. The real-world tests — a live
> browser, a live Blender, the live Commons API — stay opt-in, because they need
> network access and a Blender install; CI does not pretend to cover them.

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
| Interface languages (`zh-CN`, `en-US`) with a remembered choice | **implemented** — picker in Settings; 131 keys in a single typed schema, so a missing or stray key fails the build rather than falling back at runtime; error text is derived from a code at render time, so switching language also re-renders a failure already on screen. In-app switching in the **packaged** Tauri window is *unverified* — the shell does not launch on the machine this was built on | `apps/desktop/src/i18n/locale.test.ts` (13), `locales.test.ts` (12), `errors.test.ts` (10), `language-switch.test.ts` (3), `views/SettingsView.test.ts` (6), `docs/development/i18n.md` |
| `filesystem` connector (read-only, allowed-root constrained) | **implemented** | `tests/integration/test_filesystem_adapter.py` |
| `browser` connector — real image search via Playwright | **implemented** | `tests/integration/test_browser_real.py` (opt-in) |
| `wikimedia` connector — public Commons API, no key, returns licence + author per image | **implemented** | `tests/integration/test_wikimedia_real.py` (opt-in), `tests/unit/test_wikimedia_api.py` |
| Reference sources: `source → connector` registry, wired at bootstrap | **implemented** — a new source needs no change to the workflow, gateway, API or UI | `tests/integration/test_reference_api.py`, `docs/decisions/0013-collection-source-registry.md` |
| Adapter developer guide (quick start, test templates, boundaries) | **implemented** | `docs/development/writing-an-adapter.md` |
| WASM plugin sandbox — any `wasm32-wasip1` language, fuel + epoch + memory limits, one authorised host-call channel, opt-in content pinning, bounded fair admission, approval-gated activation, one governing process per approval store | **implemented and tested, not wired into `Runtime`** — no adapter is a plugin, and nothing in the running app calls it. 398 tests, including adversarial, invariant, execution-governance, integrity, scheduling, lifecycle, governance-identity, withdrawal-ownership, lifecycle-closure, uninstall-failure, lock-error, in-flight-close, pin-identity, revoke-recovery and aclose-reliability suites | `tests/integration/test_wasm_plugin_negative.py` (9 failure classes), `test_wasm_fair_scheduling.py`, `test_wasm_install_integrity.py`, `test_plugin_lifecycle_runtime.py`, `test_store_lock.py`, `test_withdrawal_ownership.py`, `test_lifecycle_closure.py`, `test_uninstall_failure.py`, `test_lock_errors.py`, `test_close_race.py`, `test_pin_identity.py`, `test_revoke_recovery.py`, `test_aclose_reliability.py`, `docs/decisions/0017-plugin-approval-boundary.md` |
| Plugin provenance (hash pin, install record, path rules) | **implemented, opt-in** — `require_integrity` / `require_install_record`. Detects a changed module; does **not** identify a publisher, and there is no signature or revocation list | `tests/unit/test_wasm_integrity.py`, `docs/architecture/plugin-installation.md` |
| Fair scheduling (bounded queue, round-robin, per-capability rate limit) | **implemented** — 16 waiters / 0.25 s / round-robin, refusals before execution and uncharged. Bounded waiting, **not** strict fairness; per host instance | `tests/integration/test_wasm_fair_scheduling.py`, `docs/architecture/host-capability-execution.md` |
| Plugin approval lifecycle (discover → install → approve → activate → revoke → uninstall) | **implemented** — `requested`, `approved` and `granted` are three separate sets; only `approve()` writes grants, and it reconciles the set it replaces; reinstalling identical bytes is a no-op; re-pinning clears the approval; revocation blocks queued calls and reports `queued_stopped` / `running` / `stopped=False`. An approval is a **local decision record**, not publisher identity | `tests/unit/test_plugin_lifecycle.py` (27), `tests/integration/test_plugin_lifecycle_runtime.py` (15), `tests/integration/test_plugin_approval_consistency.py` (19), `tests/integration/test_lifecycle_integrity.py` (28), `docs/architecture/plugin-lifecycle.md` |
| Read-only plugin inspection CLI (`list`, `inspect`) | **implemented** — prints pin state, requested/approved/granted side by side; exit 3 on drift. Deliberately has **no** verb that grants anything | `tests/unit/test_plugin_admin_cli.py` (9), `scripts/plugin_admin.py` |
| Plugin developer guide (ABI, manifest, build, common errors) | **implemented** | `docs/development/writing-a-wasm-plugin.md` |
| Per-invocation host-call budget for plugins | **implemented** — `limits.max_host_calls` (default 64, ceiling 1024), counted per attempt, `-7 BUDGET_EXHAUSTED`; the manifest cannot raise the ceiling | `tests/integration/test_wasm_host_budget.py` |
| Host capability execution governance — bounded pool (off the event loop), four distinct states, cost budget by declared weight, concurrency admission, shutdown that reports leftovers | **implemented** — a blocking capability no longer freezes the event loop; `timed_out`, `cancelled` and `cancellation_requested` are separate and each is tested | `tests/integration/test_wasm_host_execution.py` (14), `docs/architecture/host-capability-execution.md` |
| Real *cost measurement* for host calls | **not implemented** — `cost` is a weight a capability declares, not a measurement of CPU or memory; a capability that under-declares defeats the budget | `docs/architecture/wasm-threat-model.md` §3 |
| Interrupting a synchronous capability that is already running | **not implemented** — the pool stops *waiting* at the deadline and keeps the slot until the thread returns; only `async def` capabilities are cancellable, and `aclose()` reports what it left running | `tests/integration/test_wasm_host_execution.py::test_a_sync_capability_times_out_and_keeps_running` |
| Plugin **publisher identity** (signature / PKI / revocation) | **not implemented** — a hash proves the content is unchanged, and it cannot prove who wrote it: an attacker who can rewrite the `.wasm` can rewrite the manifest and the install record to match. An approval is a local decision record, not a credential | `docs/architecture/plugin-installation.md` §8, `docs/decisions/0017-plugin-approval-boundary.md` |
| Tamper-evident approval store | **not implemented** — the approval lives beside the grants under the same OS user; whoever can write it can approve anything. It defends against *unattended* authority, not a local attacker | `docs/architecture/plugin-lifecycle.md` §9 |
| A permissions UI for approving plugins | **not implemented** — `PluginLifecycle.approve()` is an API; the surface that shows a human the requested set does not exist | `docs/architecture/plugin-lifecycle.md` §10 |
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
corepack enable
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
  [Wikimedia Commons](docs/development/wikimedia-connector.md) ·
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
- third-party code, if it runs at all, runs as a **WebAssembly plugin**: no
  preopened directories, no network import, no process import, fuel and
  wall-clock budgets, and one host-call channel that the same permission layer
  authorises (not yet wired into the app);
- execution records and permission grants are persisted for audit.

Known gaps: there is no OS sandbox around the adapters; a same-user process can
read the child's environment (that is the trust boundary); on macOS/Linux a hard
kill of the shell can still leave the runtime running (no Job Objects there, see
`docs/decisions/0009-runtime-child-supervision.md`). The plugin sandbox caps how
many host calls a plugin may make and prices them by a weight each capability
*declares*, but that weight is not a measurement; it cannot interrupt a
synchronous capability that is already running; its content hash proves a file is
unchanged, not who wrote it; its fairness is bounded waiting (round-robin, 16
waiters, 0.25 s), not a guarantee; and it cannot defend against timing side
channels — all recorded, with their evidence, in
`docs/architecture/wasm-threat-model.md`.

## Repository layout

```
apps/desktop            Tauri 2 + Vue 3 + TypeScript shell
runtime/                Python core runtime and control API
packages/{contracts,adapter-sdk,mcp-gateway,plugin-sdk,plugin-wasm}
adapters/{browser,wikimedia,blender,filesystem}
plugins/anime-modeling
examples/wasm-plugins/reference-metadata   example plugin + committed .wasm
scripts/{ci_local.py,ui_smoke.py}
tests/{unit,contract,integration,e2e}
docs/{architecture,decisions,development}
```

## Commands

| Task | Command |
| --- | --- |
| Install exactly the lockfile | `uv sync --all-packages --locked` |
| Python lint | `uv run ruff check .` |
| Python format check | `uv run ruff format --check .` |
| Python tests | `uv run pytest` |
| Real browser tests | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_browser_real.py` |
| Real Commons tests | `DESKWEAVE_NETWORK_TESTS=1 uv run pytest tests/integration/test_wikimedia_real.py` |
| Real Blender tests | `DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py` |
| WASM plugin sandbox tests | `uv run pytest tests/unit/test_wasm_*.py tests/contract/test_wasm_plugin_contract.py tests/integration/test_wasm_*.py` |
| Rebuild the example WASM plugin | `uv run python scripts/build_wasm_plugin.py` (needs `rustup target add wasm32-wasip1`) |
| Full chain | `DESKWEAVE_NETWORK_TESTS=1 DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/e2e` |
| Frontend build | `corepack pnpm build` |
| CI, locally (same steps as GitHub Actions) | `uv run python scripts/ci_local.py` |
| Frontend tests | `corepack pnpm test` |
| Frontend lint / format | `corepack pnpm lint` / `corepack pnpm format:check` |
| Rust check | `cargo check --all-targets --locked` (in `apps/desktop/src-tauri`) |
| Rust format check | `cargo fmt --check` |
| Rust bridge tests | `cargo test --lib --locked` |
| MCP server | `uv run python -m deskweave_mcp_gateway` |
| Real UI smoke | `uv run python scripts/ui_smoke.py` |

## License

MIT — see [LICENSE](LICENSE).
