"""Blender connector: creates a reference plane from confirmed artifacts.

Execution model
---------------
The adapter drives a **headless Blender subprocess** running a fixed, bundled
script. It never imports ``bpy`` into the runtime process and never evaluates code
supplied by a caller:

* the only inputs are artifact ids (resolved to paths by the runtime's artifact
  store) and a few numeric/string parameters;
* the script reads a JSON config file, builds the planes, saves a ``.blend`` and
  prints a machine-readable result;
* a **second** Blender process re-opens the saved file and reports its contents,
  so success means "objects exist in the file", not "the process exited 0".

Re-running the same task converges: the output path is derived from the task id and
the script replaces objects carrying the same generated names instead of adding
duplicates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from deskweave_adapter_sdk import Adapter, PermissionChecker, ResourceResolver
from deskweave_contracts import (
    AdapterConnectionError,
    AdapterDescriptor,
    AdapterExecutionError,
    AdapterStatus,
    Capability,
    OperationRequest,
    PermissionScope,
    RiskLevel,
    ValidationError,
)

from .runner import BlenderInfo, BlenderRunner, ensure_output_path

PLANE_CAPABILITY = "create_reference_plane"

_SINGLE_IMAGE_MAX_HEIGHT = 4.0
_MAX_ARTIFACTS = 20

BLENDER_CAPABILITIES: list[Capability] = [
    Capability(
        name=PLANE_CAPABILITY,
        description=(
            "Create textured reference planes for confirmed image artifacts and save a .blend. "
            "Accepts artifact ids only; file paths are resolved by the runtime."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    "minItems": 1,
                    "maxItems": _MAX_ARTIFACTS,
                },
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "height": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": _SINGLE_IMAGE_MAX_HEIGHT,
                    "default": 1.0,
                },
                "gap": {"type": "number", "minimum": 0, "maximum": 5, "default": 0.15},
            },
            "required": ["artifact_ids"],
            "additionalProperties": False,
        },
        risk=RiskLevel.HIGH,
        scope=PermissionScope.BLENDER_CONTROL,
        requires_confirmation=True,
    ),
]


class CreatePlaneRequest(BaseModel):
    """Second validation gate, so a schema/runtime drift cannot slip through."""

    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[str] = Field(min_length=1, max_length=_MAX_ARTIFACTS)
    name: str = Field(default="ReferencePlane", min_length=1, max_length=64)
    height: float = Field(default=1.0, gt=0, le=_SINGLE_IMAGE_MAX_HEIGHT)
    gap: float = Field(default=0.15, ge=0, le=5)


class BlenderAdapter(Adapter):
    """Connector to a local Blender installation.

    Also implements :class:`~deskweave_adapter_sdk.RecoverableOutput`: a runtime
    that restarts mid-execution can ask whether the scene was already written and
    re-verify it, instead of blindly running Blender again.
    """

    def __init__(
        self,
        *,
        adapter_id: str = "blender",
        permission_checker: PermissionChecker | None = None,
        resource_resolver: ResourceResolver | None = None,
        output_dir: Path | None = None,
        executable: Path | None = None,
        runner: BlenderRunner | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Blender",
            version="0.1.0",
            description="Creates reference planes by driving a headless Blender subprocess.",
            capabilities=BLENDER_CAPABILITIES,
        )
        super().__init__(
            descriptor,
            permission_checker=permission_checker,
            resource_resolver=resource_resolver,
            logger=logger,
        )
        self._output_dir = Path(output_dir) if output_dir is not None else None
        self._runner = runner or BlenderRunner(executable)

    # -- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Probe the installation so misconfiguration fails at connect time."""
        try:
            info = self._runner.info()
        except AdapterConnectionError:
            self._set_status(AdapterStatus.ERROR)
            raise
        self._logger.info("Blender %s at %s", info.version, info.executable)
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        """No long-lived process is held, so this is just a status reset."""
        self._set_status(AdapterStatus.DISCONNECTED)

    # -- recovery ---------------------------------------------------------
    async def find_output(self, *, task_id: str, arguments: dict[str, Any]) -> Path | None:
        """Return the ``.blend`` a previous attempt already wrote, if any.

        The path is derived exactly as :meth:`_run` derives it (task id + name),
        so a restart can answer "was a scene produced?" without re-running
        Blender and without trusting anything the caller sends.
        """
        if self._output_dir is None:
            return None
        try:
            payload = CreatePlaneRequest.model_validate(arguments)
        except PydanticValidationError:
            return None
        output = ensure_output_path(self._output_dir, task_id, payload.name)
        if not output.is_file() or output.stat().st_size == 0:
            return None
        return output

    async def verify_output(self, path: Path, *, arguments: dict[str, Any]) -> dict[str, Any]:
        """Re-open a recovered file in Blender and check it holds the planes.

        This is deliberately the same independent check the normal path performs:
        the file opening successfully is not enough, it must contain one textured,
        non-degenerate object per requested artifact.
        """
        payload = CreatePlaneRequest.model_validate(arguments)
        scene = await self._run_script("verify_scene.py", argument="--file", value=str(path))
        usable = [
            entry
            for entry in scene.get("objects", [])
            if entry.get("images")
            and any(dimension > 0 for dimension in entry.get("dimensions", []))
        ]
        if len(usable) < len(payload.artifact_ids):
            raise AdapterExecutionError(
                "the recovered scene does not contain the expected textured planes",
                details={
                    "blend_file": str(path),
                    "expected": len(payload.artifact_ids),
                    "found": len(usable),
                },
            )
        return {
            "opened": True,
            "object_count": scene.get("object_count"),
            "verified_objects": len(usable),
            "recovered": True,
        }

    # -- execution --------------------------------------------------------
    async def _run(self, request: OperationRequest) -> dict[str, Any]:
        if request.capability != PLANE_CAPABILITY:
            raise AdapterExecutionError(
                f"capability {request.capability!r} is not implemented by the Blender connector",
                details={"adapter_id": self.adapter_id},
            )

        try:
            payload = CreatePlaneRequest.model_validate(request.arguments)
        except PydanticValidationError as exc:
            raise ValidationError(
                f"invalid reference-plane request: {exc.error_count()} problem(s)",
                details={"errors": [error["msg"] for error in exc.errors()]},
            ) from exc

        output_dir = self._output_dir
        if output_dir is None:
            raise AdapterExecutionError(
                "the Blender connector has no output directory configured",
                details={"adapter_id": self.adapter_id},
            )

        images = await self._resolve_images(payload.artifact_ids)
        output = ensure_output_path(output_dir, request.task_id, payload.name)

        info = self._runner.info()
        config_path = self._write_config(payload, images, output)
        try:
            created = await self._run_script(
                "create_reference_plane.py", argument="--config", value=config_path
            )
        finally:
            Path(config_path).unlink(missing_ok=True)
        blend_file = Path(str(created.get("blend_file") or output))
        if not blend_file.is_file():
            raise AdapterExecutionError(
                "Blender reported success but no .blend file was written",
                details={"expected": str(blend_file)},
            )

        verification = await self._verify(blend_file, created, payload)
        return self._summarise(payload, info, blend_file, created, verification)

    # -- helpers ----------------------------------------------------------
    async def _resolve_images(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve authorised artifacts to paths, rejecting anything unreadable."""
        resolved: list[dict[str, Any]] = []
        for artifact_id in artifact_ids:
            path = await self.resolve_artifact(artifact_id)
            if not path.is_file():
                raise AdapterExecutionError(
                    f"artifact {artifact_id!r} has no readable file",
                    details={"artifact_id": artifact_id},
                )
            resolved.append({"artifact_id": artifact_id, "path": str(path)})
        return resolved

    async def _run_script(self, script: str, *, argument: str, value: str) -> dict[str, Any]:
        """Run a bundled Blender script off the event loop."""
        return await asyncio.to_thread(
            self._runner.run_script, script, argument=argument, value=value
        )

    def _write_config(
        self, payload: CreatePlaneRequest, images: list[dict[str, Any]], output: Path
    ) -> str:
        config = {
            "base_name": payload.name,
            "images": images,
            "output_path": str(output),
            "height": payload.height,
            "gap": payload.gap,
        }
        handle, path = tempfile.mkstemp(suffix=".json", dir=output.parent)
        try:
            with open(handle, "w", encoding="utf-8") as stream:
                json.dump(config, stream)
        except OSError as exc:
            Path(path).unlink(missing_ok=True)
            raise AdapterExecutionError(
                f"could not write the Blender job config: {exc}",
                details={"path": path},
            ) from exc
        return path

    async def _verify(
        self, blend_file: Path, created: dict[str, Any], payload: CreatePlaneRequest
    ) -> dict[str, Any]:
        """Re-open the file in a fresh Blender and check the objects really exist."""
        scene = await self._run_script("verify_scene.py", argument="--file", value=str(blend_file))
        by_name = {entry["name"]: entry for entry in scene.get("objects", [])}
        expected = [entry["name"] for entry in created.get("objects", [])]
        missing = [name for name in expected if name not in by_name]
        empty = [
            name
            for name in expected
            if not any(dimension > 0 for dimension in by_name.get(name, {}).get("dimensions", []))
        ]
        untextured = [name for name in expected if not by_name.get(name, {}).get("images")]

        problems: list[str] = []
        if missing:
            problems.append(f"missing objects: {missing}")
        if empty:
            problems.append(f"zero-size objects: {empty}")
        if untextured:
            problems.append(f"objects without an image texture: {untextured}")
        if len(expected) != len(payload.artifact_ids):
            problems.append(
                f"created {len(expected)} object(s) for {len(payload.artifact_ids)} artifact(s)"
            )
        if problems:
            raise AdapterExecutionError(
                "Blender scene verification failed: " + "; ".join(problems),
                details={"blend_file": str(blend_file), "problems": problems},
            )
        return {
            "opened": True,
            "object_count": scene.get("object_count"),
            "verified_objects": len(expected),
        }

    @staticmethod
    def _summarise(
        payload: CreatePlaneRequest,
        info: BlenderInfo,
        blend_file: Path,
        created: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        objects = created.get("objects", [])
        return {
            # ``blend_file`` is consumed by the runtime (turned into an artifact) and
            # is never forwarded to the UI or the agent.
            "blend_file": str(blend_file),
            "plane": {
                "name": payload.name,
                "image_count": len(objects),
                "objects": [entry["name"] for entry in objects],
                "dimensions_px": [entry.get("pixels") for entry in objects],
            },
            "blender_version": info.version,
            "verification": verification,
            "idempotent_reuse": bool(created.get("reopened_existing_file")),
        }


__all__ = [
    "BLENDER_CAPABILITIES",
    "PLANE_CAPABILITY",
    "BlenderAdapter",
    "CreatePlaneRequest",
]
