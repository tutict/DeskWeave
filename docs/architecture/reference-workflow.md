# Reference Workflow (Phase 1.5)

Status: Phase 1 implemented the loop end to end on Windows 11 with a real browser
and a real Blender 5.2 installation. Phase 1.5 added process supervision, token
storage, restart recovery and `.blend` observability without changing the loop
itself — see §9, §10 and `docs/decisions/0009`–`0012`. Phase 2 added a second image
source (`wikimedia`) and therefore a source registry: the workflow is unchanged, only
the name → connector mapping is new (§11,
`docs/decisions/0013-collection-source-registry.md`).

This is the first workflow in DeskWeave that actually does something outside the
runtime process. It answers one question honestly:

> Can an agent collect real reference images, hand them to a human for a decision,
> and only then let a desktop application act on exactly that decision?

## 1. The loop

```
Codex / agent                       DeskWeave runtime                     User
      │
      │ MCP: collect_reference_images(query, limit, source)
      ├──────────────────────────────► ReferenceService.start_collection
      │◄── { task_id, status } ───────┤  (returns immediately, no blocking)
      │                               │
      │                               ├──► BrowserAdapter.collect_reference_images
      │                               │      Playwright → Bing image search page
      │                               │◄────── candidates (url, page, title, w×h)
      │                               │
      │                               ├──► ImageDownloader  (SSRF guard, byte cap,
      │                               │      magic sniffing, atomic write)
      │                               │
      │                               ├──► ArtifactStore.import_image(..., task_id)
      │                               │      → artifact rows + files under <data>/artifacts
      │                               │
      │                               └──► task = waiting_for_confirmation
      │                                       result.candidates / result.failures
      │
      │ MCP: get_reference_task(task_id)          ┌─────────────────────────────┐
      ├──────────────────────────────────────────►│ desktop UI                  │
      │◄── ids + metadata + failures ─────────────┤ previews via artifact API   │
      │                                           │ user selects 1..n images    │
      │                                           └──────────────┬──────────────┘
      │                                                          │ POST /tasks/{id}/confirm
      │                                                          ▼
      │                                            ReferenceService.confirm
      │                                              • task must be waiting
      │                                              • artifacts must belong to it
      │                                              • one-shot permission allowance
      │                                              ▼
      │                                            BlenderAdapter.create_reference_plane
      │                                              • controlled bpy script
      │                                              • second process re-opens the .blend
      │                                                          │
      │ MCP: get_reference_task(task_id)                         ▼
      └───────────────────────────────────────  task = succeeded (plane, .blend artifact)
```

Nothing in the diagram is a mock. The only optional part is the desktop shell:
the same API is served to a browser-based dev session (see §5).

## 2. Trust boundary

The agent can start work and read results. It cannot decide anything about the
user's machine.

| Rule | Where it is enforced |
| --- | --- |
| The agent cannot confirm a selection | There is no confirm tool. `tests/integration/test_mcp_reference_tools.py` asserts no tool name contains `confirm`. |
| Passing `confirmed=true` (or any undeclared field) grants nothing | Every capability schema is `additionalProperties: false`; extra arguments are rejected with `validation_error` before permission is evaluated. |
| The target adapter is unreachable before confirmation | `PermissionManager` denies `blender:control` (`risk=high`, `requires_confirmation=true`); the workflow only injects a one-shot allowance *inside* `confirm()`, after the state check. |
| An artifact id is not a capability | `ResourceResolver` maps `artifact_id → path` inside the runtime; adapters never accept a path from the caller and cannot read outside the artifact root. |
| A stale page cannot execute twice | `confirm()` takes a per-task lock; a task that is already `running` with a confirmation returns `conflict`; a `succeeded` task replays its stored result. |
| A cancelled or finished task cannot be confirmed | State machine (`waiting_for_confirmation` is the only source state) — `conflict` otherwise. |

`confirmed=true` is *data*, not authorisation: the field does not exist in
`ConfirmRequest`, and `ConfirmRequest` also forbids extra fields.

## 3. Task and result shape

One task per collection, one `workflow_run` per task (`reference-collection`).

```text
TaskRecord.status          pending → running → waiting_for_confirmation
                                                │
                            ┌───────────────────┼────────────────────┐
                            ▼                   ▼                    ▼
                        succeeded            failed             cancelled
```

`task.result` is written **before** any work starts, so an interrupted task is
diagnosable from the database alone:

```json
{
  "request": {
    "query": "anime character reference sheet",
    "limit": 4,
    "source": "bing",
    "target": "blender",
    "plane_name": "SmokePlane"
  },
  "query": "anime character reference sheet",
  "source": "bing",
  "candidates": ["art_…", "art_…"],
  "failures": [{"url": "https://…", "reason": "TimeoutError: timed out"}]
}
```

`candidates` appears only once the collection reached the end; a task killed
mid-download has `request` and nothing else, which is exactly what lets recovery
tell "never started" from "stopped early" (§9).

After a confirmation the same object gains:

```json
{
  "confirmation": {
    "artifact_ids": ["art_…"],
    "target": "blender",
    "plane_name": "SmokePlane",
    "confirmed_at": "2026-10-20T10:03:36.677000+00:00"
  },
  "execution": {
    "ok": true,
    "blend_artifact_id": "art_…",
    "plane": {"name": "SmokePlane", "image_count": 1, "objects": ["SmokePlane_01"], "dimensions_px": [[1376, 768]]},
    "blender_version": "5.2.0",
    "verification": {"opened": true, "object_count": 1, "verified_objects": 1},
    "idempotent_reuse": false
  }
}
```

`execution.blend_file` exists internally (it is an absolute path) and is stripped
by both the control API serialiser and the MCP tool. Tests assert the absence of
`file://` and of the data directory in every payload.

## 4. Control API

All routes require the runtime token (`Authorization: Bearer …` or
`X-DeskWeave-Token`), bind to loopback, and answer JSON errors as
`{"error": {"code", "message", "details"}}`.

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | 200 | adapters + capabilities |
| `GET` | `/adapters` | 200 | descriptors, no paths |
| `GET` | `/tasks?status=` | 200 | `ReferenceTaskView` list |
| `GET` | `/tasks/{id}` | 200 | `ReferenceTaskView` |
| `POST` | `/tasks` | 202 + `Location` | starts a collection, returns immediately |
| `POST` | `/tasks/{id}/confirm` | 200 | runs the target, returns the final view |
| `POST` | `/tasks/{id}/cancel` | 200 | state change only |
| `GET` | `/artifacts/{id}` | 200 | metadata (no `uri`) |
| `GET` | `/artifacts/{id}/preview` | 200 | raw bytes, `CSP: default-src 'none'; sandbox` |
| `POST` | `/artifacts/{id}/open` | 200 | `{"action": "reveal"\|"open"}`; user-initiated side effect, resolves the path inside the runtime |
| `GET` | `/events` | 200 | bounded event history |
| `POST` | `/shutdown` | 200 | used by tests and the packaging path |

The `open` route is the only route that causes something to happen outside the
process. It takes an artifact id and an action, never a path; the response
contains `{artifact_id, kind, media_type, action, opened}` and no path. The
Tauri bridge allows exactly `POST /artifacts/{id}/open` and nothing else under
that prefix (`apps/desktop/src-tauri/src/bridge.rs`).

Error mapping: `validation_error` 400, `unauthorized` 401, `not_found` 404,
`conflict` 409, `permission_denied` / `browser_access_denied` 403,
`unsafe_resource` 422, `adapter_timeout` 504, `adapter_connection` 503, other
`adapter_*` 502.

Guards: request body capped at 64 KiB and rejected from `Content-Length` before
reading; ids must match `[A-Za-z0-9_-]{1,64}`; `confirm` has a 240 s timeout,
everything else 30 s.

## 5. Browser transport vs. Tauri IPC

| Client | Transport | Token exposure |
| --- | --- | --- |
| Desktop shell | `runtime_request` (Rust `reqwest`) | token stays in Rust |
| `pnpm dev` / UI smoke | `fetch` with `Authorization` header | token comes from `VITE_*` build-time env — dev only |

Because the browser transport is supported, the control API answers CORS
**preflight only for loopback origins** (`http(s)://localhost|127.0.0.1|::1`). A
page on any other origin gets `401` with no CORS headers, so it can neither read
responses nor send credentialed requests.

## 6. Artifacts

| Property | Value |
| --- | --- |
| id | `art_` + 32 hex chars, generated by the runtime |
| file name | `artifacts/<id>.<ext>`, extension derived from sniffed bytes |
| metadata | `source`, `source_page`, `original_url`, `title`, `sha256`, `width`, `height`, `extra.thumbnail_url`; `ArtifactRef.created_at` records the collection time |
| limits | 8 MiB per file, 20 s deadline, 3 redirects, max 20 candidates per request |
| accepted | PNG, JPEG, GIF, BMP, WebP (magic bytes, not `Content-Type`) |
| rejected | SVG, AVIF, HTML, anything whose bytes do not match a known signature |

Files are written to `<data>/artifacts` with a `.part` suffix and `os.replace`, so
a crashed download never looks like a valid artifact. `ArtifactStore.resolve_path`
re-checks containment on every read; a symlink pointing outside the root is
refused, not followed.

## 7. Failure semantics

Every failure mode has a named outcome; none of them is a silent success.

| Situation | Result |
| --- | --- |
| Search page blocked (captcha / consent wall) | task `failed`, `browser_access_denied`, actionable message |
| Layout changed (no result cards) | task `failed`, message names the selector and the URL |
| No candidates found | task `failed`, `no_candidates` |
| Some candidates undownloadable | task **stays** `waiting_for_confirmation`; each failure is listed with its URL and reason |
| All candidates undownloadable | task `failed`, `no_candidates` ("no candidate image could be downloaded"), failures in `details` |
| Collection exceeds 120 s | task `failed`, `adapter_timeout` |
| Blender missing / too old | `adapter_connection` at confirm time; task `failed` with `target_execution_failed` |
| Blender script raises, or produces no file | task `failed`, stderr tail preserved in the error message |
| User cancels | task `cancelled`; an in-flight collection is cancelled cooperatively |

## 8. Concurrency and idempotency

- `start_collection` returns before the browser runs; the agent polls
  `get_reference_task` instead of waiting on a tool call.
- `confirm` is single-flight per task. Two simultaneous confirms: one wins, the
  other gets `conflict` (or the stored result if it arrives after success).
- `create_reference_plane` is idempotent by object naming: re-running for the same
  plane name deletes `{name}_*` objects before creating them, reports
  `idempotent_reuse: true` when the `.blend` already existed, and leaves the object
  count unchanged (`tests/integration/test_blender_real.py`).
- Cancel during collection cancels the background `asyncio` task and awaits it, so
  the cancellation is observed inside the download loop rather than after the
  fact; a task whose confirmed Blender run is already in flight is **not**
  cancellable and answers `conflict`.

## 9. Startup recovery

The runtime reconciles interrupted tasks **before** it starts serving
(`runtime.recover()`, called from `service/__main__.py`). It never executes
anything: every uncertain case goes back to the confirmation gate with a recorded
reason. Rules and rejected alternatives: `docs/decisions/0011-restart-recovery.md`.

| State found at startup | Action |
| --- | --- |
| `pending` | `failed` / `never_started` — it never reached an adapter |
| `running`, no confirmation, image artifacts exist | `waiting_for_confirmation` / `resumed_partial_collection` |
| `running`, no confirmation, no image artifacts | `failed` / `interrupted` |
| `running`, confirmation recorded, adapter can prove the output | `succeeded` / `resumed_execution`, `execution.recovered = true` |
| `running`, confirmation recorded, output unverifiable or missing | `waiting_for_confirmation` / `confirmation_required` |
| `waiting_for_confirmation`, `succeeded`, `failed`, `cancelled` | untouched (idempotent second run finds nothing) |

Every decision appends `task.result.recovery = {action, detail, at}` and is
logged as `recovery: task <id> <action> -> <status> (<detail>)`. `/health` reports
the batch under the `recovery` component (`not run yet`, `no interrupted tasks
found`, or `N task(s) reconciled at startup (…, …)`), and degrades when any task
hit an error. Recovery is bounded: 20 tasks per batch, 15 s to look up an output,
90 s to verify one, and a failure never blocks startup.

The "did I already write the file?" question is answered by the adapter, not
guessed by the runtime: `RecoverableOutput.find_output` / `verify_output`
(`packages/adapter-sdk/src/deskweave_adapter_sdk/recovery.py`). The Blender adapter
re-opens the `.blend` in a second process before it claims success, so a truncated
or empty file lands in `confirmation_required`, not `succeeded`.

## 10. Blender output observability

A `.blend` is not an image, and the UI says so instead of showing a placeholder
screenshot (`docs/decisions/0012-blender-output-observability.md`).

| Question | Answer |
| --- | --- |
| Where is the file? | `<data>/artifacts/plane-<task_id>.blend`, registered as a normal artifact by id |
| How does the UI find it? | `execution.blend_artifact_id` → lookup in the existing artifact list, no path in the view model |
| What can the user see? | artifact id, size, creation time, media type, object names, verification counts |
| What can the user do? | "Show in folder" (`reveal`) and "Open in Blender" (`open`) — one route, id + action only |
| What if the artifact is gone? | the block reports the missing artifact and hides both actions |
| Thumbnail? | **not implemented**. A representative render needs a camera, a light and a background rig this scene does not have; a fake preview would be worse than none |

Failures are named: `artifact_open_failed` (the OS refused to launch),
`not_implemented` 501 (unknown platform, no reveal/open strategy),
`not_found` (artifact or file missing), `unsafe_resource` (artifact has no local
file). None of them is reported as success.

## 11. Sources and their connectors

A `source` is a name in the request; which connector serves it is runtime
configuration. Phase 2 added `wikimedia` without touching the workflow logic.

```
CollectionRequest(source="wikimedia")
        │
        ├─ ValidationError at the edge          (name not in ReferenceSource)
        ▼
ReferenceService.adapter_id_for_source()  ← runtime.register_collection_source(...)
        │                                     "bing" → browser (built-in default)
        │                                     "wikimedia" → wikimedia (bootstrap)
        ├─ AdapterConnectionError, 503          (name is valid but nothing serves it:
        │                                        details.source, details.registered_sources)
        ▼
ProviderAdapter.execute(collect_reference_images)
```

| Source | Connector | Transport | Confirmation |
| --- | --- | --- | --- |
| `bing` | `browser` | Playwright drives a real search page, then scrapes image results | none — `browser:control` reads public pages, writes nothing outside the artifact sandbox |
| `wikimedia` | `wikimedia` | HTTPS GET to `commons.wikimedia.org/w/api.php` (`generator=search`, `gsrnamespace=6`) | none — `READ_ONLY` + `network` scope |

Both return the same candidate shape, which is why the download, artifact, confirm
and Blender steps are untouched by the choice of source. The Wikimedia connector
adds what Commons uniquely gives: `license` and `artist` come back from
`extmetadata` and are stored as artifact provenance, so a reference carries its
attribution instead of losing it.

Two behaviours are deliberate:

- **It does not fall back across sources.** If Wikimedia is unreachable, the task
  fails with `adapter_connection_error`; it does not quietly ask Bing. A source the
  user chose is the source that answers.
- **It does not retry an API call.** The real integration test retries a TLS
  handshake because it needs a stable answer; the product does not, because a
  silent retry hides an outage from the person waiting for the result.

## 12. Deliberately not here

- No WebSocket push: the UI polls every 2 s while any task is running. A push
  channel is only worth adding together with a real change feed (see
  `docs/decisions/0003-mcp-gateway-boundary.md`).
- No retry policy: a failed task stays failed. The user starts a new collection.
- No generic search aggregator: two named sources, each with its own parser and its
  own failure modes, and no cross-source fallback (§11). See
  `docs/decisions/0006-browser-reference-source.md` and
  `docs/decisions/0013-collection-source-registry.md`.
- No arbitrary Blender scripting from tool arguments: only a fixed script with a
  JSON config. See `docs/decisions/0008-blender-headless-execution.md`.
