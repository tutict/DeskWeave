"""Re-open a saved .blend and report what is actually inside it.

Used by the adapter as an independent verification step: the creation script
claims it made objects, this script proves they exist in the file on disk.

    blender -b --factory-startup --python verify_scene.py -- --file <blend> --json-out <path>

Result: ``DESKWEAVE_SCENE <json>`` on stdout (and in the ``--json-out`` file).
"""

from __future__ import annotations

import json
import os
import sys

SCENE_PREFIX = "DESKWEAVE_SCENE "


def _fail(message: str) -> None:
    print(f"DESKWEAVE_ERROR {message}", file=sys.stderr)
    sys.exit(3)


def _parse_args() -> tuple[str, str | None]:
    argv = sys.argv
    if "--" not in argv:
        _fail("missing '--' separator")
    args = argv[argv.index("--") + 1 :]
    values: dict[str, str] = {}
    index = 0
    while index < len(args):
        key = args[index]
        if key not in {"--file", "--json-out"} or index + 1 >= len(args):
            _fail(f"unexpected argument {key!r}")
        values[key] = args[index + 1]
        index += 2
    if "--file" not in values:
        _fail("--file is required")
    return values["--file"], values.get("--json-out")


def main() -> None:
    import bpy

    path, json_out = _parse_args()
    if not os.path.isfile(path):
        _fail(f"blend file not found: {path}")

    bpy.ops.wm.open_mainfile(filepath=path)

    objects = []
    for obj in bpy.data.objects:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "dimensions": [round(value, 6) for value in obj.dimensions],
            "location": [round(value, 6) for value in obj.location],
            "images": [],
        }
        for slot in getattr(obj, "material_slots", []):
            material = slot.material
            if material is None or not material.use_nodes:
                continue
            for node in material.node_tree.nodes:
                image = getattr(node, "image", None)
                if image is not None:
                    entry["images"].append(image.name)
        objects.append(entry)

    payload = {
        "ok": True,
        "blend_file": path,
        "blender_version": bpy.app.version_string,
        "object_count": len(objects),
        "objects": objects,
    }
    encoded = json.dumps(payload, sort_keys=True)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    print(SCENE_PREFIX + encoded)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the message must reach the adapter, never a traceback
        _fail(f"{type(exc).__name__}: {exc}")
