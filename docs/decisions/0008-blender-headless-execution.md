# ADR 0008 — Driving Blender headlessly, and proving it actually ran

- Status: accepted
- Date: 2026-10-20
- Phase: 1

## Context

`blender.create_reference_plane` must place the confirmed images into a Blender
scene. Two things can go wrong in ways that look like success:

- the process starts and exits 0 while doing nothing (script not executed, wrong
  file opened, exception swallowed by a GUI-less run);
- the process writes a `.blend` that does not contain what was asked for
  (missing objects, zero-size planes, no image texture).

Reporting "created" in either case would be the exact fake-success the project
forbids.

Measured environment: Blender 5.2.0 LTS at
`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`. A `blender --background
--factory-startup --python script.py` run works; a 4.4 install directory on the
same machine has no `blender.exe`, which is why discovery checks the executable
rather than the folder name.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **Fixed `bpy` script + JSON config, then verify in a second process** | No code path from tool arguments; verification is independent of the code that wrote the file | Two Blender launches per confirmation (~4 s each) |
| `bpy` as a Python module inside the runtime | No subprocess | Blender's Python is a different ABI; `bpy` is not installable on the runtime's interpreter; would pin the runtime to one Blender build |
| Generated Python from arguments | Flexible | Arbitrary code execution through tool arguments — forbidden by the security model |
| Trust the script's stdout as proof | Cheapest | The exact failure mode above: stdout is self-reported |
| GUI automation (keyboard/mouse) | Works with a visible Blender | Fragile, needs a desktop session, unverifiable |

## Decision

1. **Controlled script, data-only input.** `create_reference_plane.py` reads a JSON
   config file (`--config <path>`) written by the adapter from
   `CreatePlaneRequest`. Arguments can name artifacts and a plane name; they can
   never contain Python, a path, or a `bpy` expression.
2. **Explicit launch contract** (`BlenderRunner.run_script`):
   `blender --background --factory-startup --python-exit-code 3 --python <script> -- <arg> <value>`.
   `--factory-startup` isolates the run from the user's preferences and add-ons;
   `--python-exit-code 3` makes an uncaught script exception a non-zero exit.
3. **Result marker, not exit code alone.** The script prints exactly one line
   `DESKWEAVE_RESULT {json}`; the runner parses the last such line. Missing marker
   or non-zero exit → `AdapterExecutionError`; exceeding the 150 s timeout →
   `AdapterTimeoutError`. Both are surfaced with the process output tail.
4. **The `.blend` must exist on disk** after the run, or the operation fails. The
   runtime then registers it as a `model` artifact owned by the task.
5. **Independent verification.** `verify_scene.py` runs in a **second, fresh
   Blender process** that re-opens the saved file and dumps every object with its
   type, dimensions, location and image users. The adapter then asserts:
   every created object exists, has a non-zero dimension, has an image texture,
   and the object count equals the number of confirmed artifacts.
   Verification failure fails the task — the file is not silently accepted.
6. **Idempotency by naming.** Objects are named `{base}_{artifact-id-suffix}`; a
   rerun for the same plane name deletes existing `{base}_*` objects before
   creating them, reuses the existing `.blend`, and reports
   `idempotent_reuse: true`. MCP retries therefore converge instead of stacking
   duplicates.
7. **No global state.** `--factory-startup` plus a file-scoped save means the run
   cannot pick up, or leave behind, the user's scene. Nothing is written outside
   the task's output directory.

## Reasons

1. Verification in a second process is the only way to distinguish "the script
   said it did" from "the file contains it". It costs one extra launch per
   confirmation — cheap next to being wrong.
2. `--factory-startup` is what makes the operation reproducible: the same input
   produces the same scene on a machine with any add-ons installed.
3. Data-only config keeps the "no arbitrary code execution" rule intact while
   still allowing the plane name, height and spacing to be parameterised.
4. Naming-based idempotency needs no extra table and survives restarts, which
   matches how the rest of the runtime treats state (SQLite + files, no memory).

## Consequences

- Two Blender launches per confirmation; `blender` must be installed (see
  `docs/development/blender-connector.md`). Without it the capability reports
  `adapter_connection`, and the task fails with a message naming the paths that
  were checked.
- The script sets the image as a texture on the Principled BSDF base colour with
  alpha; it is not a full material system. Anything more belongs to the
  `anime-modeling` plugin, not to the connector.
- Verified against Blender 5.2 only. The minimum accepted version is 3.0
  (`MIN_VERSION`), which is a claim about the API used, not something the test
  suite proves on every minor release.

## Revisit if

- Blender ships a stable embedded-Python ABI, which would remove the subprocess;
- or scene verification becomes a general capability (then it should be its own
  read-only capability that any caller may use, instead of a private step).
