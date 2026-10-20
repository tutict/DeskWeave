# ADR 0012 — Showing a produced `.blend` without pretending it is an image

- Status: accepted
- Date: 2026-10-21
- Phase: 1.5

## Context

A confirmed task produces a `.blend` file. The Phase 1 UI showed a single line with
the artifact id, which answers "did something get created?" but not "what exactly,
how big, and can I open it?".

Constraints that shape any answer:

- a `.blend` is **not** a browser-renderable image; putting it in an `<img>` or in
  the preview endpoint's response would be a lie the browser would render as a
  broken image;
- the webview must never receive a filesystem path (ADR 0007): paths are not
  capabilities, and a compromised renderer must not be able to name one;
- opening a file is a **side effect on the user's machine** and must be triggered by
  the user, not by an agent or by a page load;
- anything shown as "success" must be verifiable.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Metadata + OS hand-off (`reveal` / `open`) via the runtime** | The runtime already owns id→path resolution and containment checks; the webview stays path-free; works with whatever Blender the user installed | No inline visual; the OS decides which app handles `.blend` |
| Render a thumbnail with `blender --background` | Visual confirmation in the UI | The scene has no camera; a correct thumbnail needs a controlled render rig (camera framing, resolution, engine, colour management) and a wall-clock budget. Getting it wrong produces a pretty picture of the wrong thing |
| Serve the `.blend` bytes through the preview endpoint | No new route | `application/x-blender` in an `<img>` is a broken image; "preview" would mean "download" |
| Put the path in the view model and call `shell.open` from the UI | Fewer moving parts | Leaks paths into the webview; every future UI bug becomes a local-file-read primitive |
| Launch Blender directly from Rust | No Python round-trip | Duplicates artifact resolution; the runtime would still have to authorise the id |

## Decision

1. **The view model gains no paths.** The UI shows what the store already exposes:
   artifact id, `size_bytes`, `created_at`, `media_type`, plus the object summary the
   adapter returned (`result.execution.plane.objects`) and whether the run was
   `recovered`.
2. **Opening is a runtime endpoint**, `POST /artifacts/{id}/open` with
   `{"action": "reveal" | "open"}`:
   - the runtime resolves the id through `ArtifactStore.resolve_path`, which
     re-checks existence **and** containment inside the allowed roots on every call;
   - non-local URIs (anything that is not `file:`/bare path) are refused with
     `unsafe_resource`;
   - `reveal` selects the file in the OS file manager (`explorer /select,<path>`,
     `open -R`, `xdg-open <dir>`); `open` asks the OS to open the file with its
     registered handler (on Windows `os.startfile`, i.e. the user's own `.blend`
     association — DeskWeave does not guess where Blender lives a second time);
   - the launched process is detached: the runtime does not wait for Blender to
     close, and a spawn failure becomes `artifact_open_failed`, never a silent
     no-op;
   - the response is `{artifact_id, kind, media_type, action, opened}` — no path.
3. **The browser bridge allows exactly one new route**:
   `POST /artifacts/{id}/open`, id-constrained in `bridge.rs` like every other route.
4. **Failure states are first-class in the UI**, each with its own message:
   the recorded artifact is gone from the store (`missing-artifact`, actions hidden),
   the file was produced but could not be registered (`blend_warning`), and the open
   call was refused (`open-error` next to the buttons).
5. **No thumbnail in this phase.** A `.blend` therefore shows no image, and the UI
   says so ("A .blend file cannot be previewed in the browser; open it in Blender or
   reveal it on disk"). That is the honest state; a placeholder image would be the
   fake preview the project forbids.

## Consequences

- The desktop app can take the user from "task succeeded" to a scene open in Blender
  in two clicks, without ever exposing a path to the renderer.
- Reveal/open need an OS handler. On a machine without Blender installed, `open`
  fails with a structured error (`artifact_open_failed`) instead of doing nothing.
- **Not implemented, and listed as such in the capability matrix:** inline
  thumbnails of the produced scene, and "open in a specific Blender build" (the
  existing `DESKWEAVE_BLENDER` discovery is used for *execution*, not for opening —
  mixing the two would make an execution setting silently change a UI action).
- Tests: `tests/unit/test_artifact_opener.py` (plan per platform, path never
  returned, unknown action, missing file, remote URI, spawn failure),
  `tests/integration/test_service_api.py::test_open_artifact_route_never_leaks_a_path`,
  `bridge.rs::tests` (route allowed/rejected), and four UI tests covering metadata,
  the two actions, a refused open, the missing artifact and the unregistered file.
