"""Create reference planes in Blender from already-authorised image files.

This script is executed by Blender's own Python:

    blender -b --factory-startup --python create_reference_plane.py -- --config <json>

It is a *fixed* script: the only input is a JSON config file written by the
adapter, and the config contains no code. File paths in it were resolved from
artifact ids by the runtime, so this script never decides what to read.

Output: one ``DESKWEAVE_RESULT <json>`` line on stdout. Everything else Blender
prints is ignored by the caller.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys

RESULT_PREFIX = "DESKWEAVE_RESULT "


def _fail(message: str) -> None:
    print(f"DESKWEAVE_ERROR {message}", file=sys.stderr)
    sys.exit(3)


def _read_config() -> dict:
    argv = sys.argv
    if "--" not in argv:
        _fail("missing '--' separator")
    args = argv[argv.index("--") + 1 :]
    if len(args) != 2 or args[0] != "--config":
        _fail("expected exactly: --config <path>")
    path = args[1]
    if not os.path.isfile(path):
        _fail(f"config file not found: {path}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_image(path: str):
    import bpy

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    image = bpy.data.images.load(path, check_existing=False)
    image.name = os.path.basename(path)
    # Pack so the .blend is portable and does not silently point at a temp file.
    with contextlib.suppress(RuntimeError):
        image.pack()
    return image


def _material_for(image, name: str):
    import bpy

    material = bpy.data.materials.new(name=f"{name}-mat")
    material.use_nodes = True
    tree = material.node_tree
    nodes = tree.nodes
    principled = nodes.get("Principled BSDF")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.location = (-400, 200)
    if principled is not None:
        tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        alpha = texture.outputs.get("Alpha")
        if alpha is not None and "Alpha" in principled.inputs:
            tree.links.new(alpha, principled.inputs["Alpha"])
        if "Alpha" in principled.inputs:
            principled.inputs["Alpha"].default_value = 1.0
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    return material


def _remove_previous(base: str) -> int:
    """Delete objects this script created before, so retries converge."""
    import bpy

    prefix = f"{base}_"
    doomed = [obj for obj in bpy.data.objects if obj.name.startswith(prefix)]
    for obj in doomed:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
    return len(doomed)


def _create_plane(name: str, image, *, location, height: float):
    import bpy

    px_width, px_height = image.size
    if not px_width or not px_height:
        raise ValueError(f"image {image.name!r} has no usable dimensions")
    width = height * (px_width / px_height)

    bpy.ops.mesh.primitive_plane_add(size=1.0, location=location)
    plane = bpy.context.active_object
    plane.name = name
    plane.data.name = f"{name}-mesh"
    plane.scale = (width, height, 1.0)
    # Stand the plane up: the default plane lies in XY, references should face -Y.
    plane.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    plane.data.materials.clear()
    plane.data.materials.append(_material_for(image, name))

    bpy.context.view_layer.objects.active = plane
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return plane, width


def main() -> None:
    import bpy

    config = _read_config()
    base = str(config["base_name"])
    images: list[dict] = list(config["images"])
    output = str(config["output_path"])
    height = float(config.get("height", 1.0))
    gap = float(config.get("gap", 0.15))

    opening = os.path.isfile(output)
    if opening:
        bpy.ops.wm.open_mainfile(filepath=output)
    else:
        # Deliberately start from an empty scene: this file belongs to DeskWeave,
        # and the factory default cube would otherwise end up in every export.
        bpy.ops.wm.read_factory_settings(use_empty=True)

    removed = _remove_previous(base)

    created = []
    cursor = 0.0
    for index, item in enumerate(images):
        image = _load_image(str(item["path"]))
        name = f"{base}_{str(item['artifact_id'])[-8:]}"
        plane, width = _create_plane(
            name,
            image,
            location=(cursor, 0.0, height / 2.0),
            height=height,
        )
        cursor += width + gap
        created.append(
            {
                "name": plane.name,
                "type": plane.type,
                "dimensions": [round(value, 6) for value in plane.dimensions],
                "pixels": [int(image.size[0]), int(image.size[1])],
                "artifact_index": index,
            }
        )

    if created:
        offset = -(cursor - gap) / 2.0
        for entry in created:
            bpy.data.objects[entry["name"]].location.x += offset

    bpy.ops.wm.save_as_mainfile(filepath=output)

    payload = {
        "ok": True,
        "blender_version": bpy.app.version_string,
        "blend_file": output,
        "base_name": base,
        "reopened_existing_file": opening,
        "removed_previous_objects": removed,
        "objects": created,
    }
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the message must reach the adapter, never a traceback
        _fail(f"{type(exc).__name__}: {exc}")
