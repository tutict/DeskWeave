# Blender connector

The `blender` connector creates textured reference planes from images the user has
confirmed, and saves them into a `.blend` file. It drives a **headless Blender
subprocess** running a fixed script; the runtime process never imports `bpy`.

| Capability | Risk | Scope | Confirmation |
| --- | --- | --- | --- |
| `create_reference_plane` | `high` | `blender:control` | **required** — enforced by the permission layer *and* by the workflow state machine |

## 1. Install Blender

Any Blender 3.0 or newer will do; the development machine uses **5.2.0 LTS**
(`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`). No add-on and no
GUI session is needed — everything runs with `--background`.

Discovery order (`find_blender` in `runner.py`):

1. `DESKWEAVE_BLENDER` — absolute path to the executable;
2. `%PROGRAMFILES%\Blender Foundation\*\blender.exe`, newest install first;
3. `%PROGRAMFILES%\Blender\blender.exe`;
4. macOS: `/Applications/Blender.app/Contents/MacOS/Blender`;
5. Linux: `/usr/bin/blender`, `/usr/local/bin/blender`, then `which blender`.

The check is on the **executable**, not the folder: a `Blender 4.4` directory
without `blender.exe` is skipped, and the error lists every path that was tried.

## 2. Configure

| Variable | Meaning |
| --- | --- |
| `DESKWEAVE_BLENDER` | Absolute path to `blender.exe` / `blender`. Optional. |
| `DESKWEAVE_BLENDER_TESTS` | Set to `1` to enable the opt-in real-Blender integration tests. |

There is no other setting. The output directory is the runtime's artifact root
(`<data_dir>/artifacts`); the file name is derived from the plane name and the
task id — a caller cannot choose a path.

At startup the adapter runs `blender --version` once and refuses to connect if the
version is below 3.0 or cannot be parsed, so "Blender is not installed" is a
startup error, not a surprise at confirmation time.

## 3. What it does

Arguments: `artifact_ids` (1–20), `name` (≤64 chars, default `ReferencePlane`),
`height` (world units, default 1.0, max 4.0), `gap` (default 0.15, max 5).

For each confirmed artifact the script:

1. loads the image from the artifact store and packs it into the `.blend`;
2. creates a plane scaled by the image aspect ratio, rotated to stand upright,
   with the transform applied;
3. builds a material with an image texture driving Principled base colour and
   alpha;
4. places the planes in a row centred on the origin;
5. saves `<plane>-<task_id>.blend` and prints one `DESKWEAVE_RESULT {json}` line.

After the save, **a second Blender process** re-opens the file
(`verify_scene.py`) and the adapter asserts that every created object exists, has
a non-zero dimension, uses an image texture, and that the object count matches the
number of confirmed artifacts. If any check fails, the operation fails — the
`.blend` is not registered as a successful artifact.

Re-running for the same plane name deletes `{name}_*` objects first, reuses the
existing file and reports `idempotent_reuse: true`. MCP retries therefore
converge.

## 4. Run

Blender is reached only through a confirmation, never directly:

```bash
# 1. start a collection and wait for waiting_for_confirmation
# 2. confirm exactly the artifacts that should be used
curl -s -X POST http://127.0.0.1:8787/tasks/<task_id>/confirm \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"artifact_ids": ["art_…"], "plane_name": "CharacterRef"}'
```

The response contains `task.status` and `task.result.execution`:

```json
{"ok": true,
 "blend_artifact_id": "art_…",
 "plane": {"name": "CharacterRef", "image_count": 1, "objects": ["CharacterRef_9bc9ecb45"], "dimensions_px": [[1376, 768]]},
 "blender_version": "5.2.0",
 "verification": {"opened": true, "object_count": 1, "verified_objects": 1},
 "idempotent_reuse": false}
```

`execution.blend_file` (the absolute path) is deliberately removed before the
payload reaches the UI or the agent.

## 5. The produced file

| Property | Value |
| --- | --- |
| Path | `<data_dir>/artifacts/plane-<task_id>.blend` (manual runs without a task: `plane-manual.blend`) |
| Artifact | registered like any other artifact; id in `execution.blend_artifact_id` |
| Metadata | size, `created_at`, media type, object names, verification counts — all from the existing artifact and execution records |
| Lifetime | until the user deletes the data directory; nothing prunes it |
| Preview | **none in the app.** A `.blend` is not an image, and the scene has no camera/light rig, so a render would be a picture of nothing in particular. The UI states this instead of showing a placeholder |
| Actions | "Show in folder" and "Open in Blender" — `POST /artifacts/{id}/open` with `{"action": "reveal"\|"open"}`; the runtime resolves the path, the webview only ever sees the id and `opened: true` |

Opening is a user side effect, so it is never triggered by a task result and never
by an agent tool. On Windows `reveal` is `explorer /select,<path>` and `open` is
`os.startfile`; macOS uses `open -R` / `open`; Linux uses `xdg-open`; any other
platform answers `not_implemented` (501) rather than guessing. A launch failure is
an `artifact_open_failed` error, not a silent no-op.

## 6. Recovery after an interruption

The adapter implements `RecoverableOutput`, which is how the runtime answers
"did the Blender run already finish?" without re-running it:

- `find_output(task_id, arguments)` returns the `.blend` for that task if it exists;
- `verify_output(path, arguments)` re-opens it with `verify_scene.py` and checks
  that the expected objects are present with an image texture.

If verification passes, startup recovery marks the task `succeeded` with
`execution.recovered = true` and registers the file as an artifact. If the file is
missing, truncated or unverifiable, the task goes back to
`waiting_for_confirmation` and the user decides — recovery never starts Blender on
its own. See `docs/decisions/0011-restart-recovery.md`.

## 7. Test

```bash
uv run pytest tests/unit/test_blender_runner.py -v                 # discovery, paths, markers
DESKWEAVE_BLENDER_TESTS=1 uv run pytest tests/integration/test_blender_real.py -v
```

The integration test launches real Blender, asserts a textured plane and a
`.blend` on disk, re-runs the same operation to prove idempotency, and checks that
an unknown artifact id is refused *before* Blender is started.

Manual check of a produced file:

```bash
blender --background --factory-startup --python-exit-code 3 \
  --python adapters/blender/src/deskweave_adapter_blender/scripts/verify_scene.py \
  -- --file "C:/path/to/ReferencePlane-task_x.blend" --json-out "C:/tmp/scene.json"
```

## 8. Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `no Blender executable found; install Blender or set DESKWEAVE_BLENDER to the full path of the blender binary` | Nothing matched discovery. `details.checked` lists every path that was tried. |
| `Blender executable does not exist: …` | `DESKWEAVE_BLENDER` points at something that is not there. |
| `Blender 2.9 is too old; 3.0+ is required` | The plane/material API differs; install a newer Blender. |
| `Blender did not finish 'create_reference_plane.py' within 150s` | First-run shader compilation on a slow disk, or a hung subprocess. Re-run once; if it persists, run the same command manually to see the output. |
| `Blender script 'create_reference_plane.py' failed (exit 3): …` | The script raised. `--python-exit-code 3` turns an uncaught exception into a non-zero exit, and the message carries the stderr tail. |
| `Blender script '…' produced no result; the scene state is unknown` | The process wrote no `DESKWEAVE_RESULT` marker, so nothing can be claimed about the scene. |
| `Blender scene verification failed: missing objects …` | The file was written but does not contain what was asked for. Treat it as a real failure and inspect the `.blend`. |
| `objects without an image texture: […]` | The image failed to pack (usually a corrupt file). Re-collect the candidate. |
| `permission_denied … requires confirmation` | The capability was called outside a user confirmation. This is the intended guard — use `POST /tasks/{id}/confirm`. |
| `unsafe_resource: refusing to write the blend file outside the output directory` | A crafted plane name tried to escape the artifact root. |
| `artifact_open_failed: could not open …` | The OS refused to launch Explorer/Blender for the artifact. The file may have been moved or the file association is broken. |
| `not_implemented: opening artifacts is not implemented on <platform>` | Only Windows, macOS and Linux have a reveal/open strategy. |
| `the recovered output did not verify (…)` in `task.result.recovery` | A previous run left a `.blend` that does not contain the expected scene. Confirm again to rebuild it. |
