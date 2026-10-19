"""Blender connector (Phase 0 skeleton).

Lifecycle and capability declarations are real; the bpy-driven work (creating
reference planes, importing material) is not implemented yet and raises
:class:`NotImplementedYetError`.
"""

from __future__ import annotations

from importlib.util import find_spec

from deskweave_adapter_sdk import Adapter, PermissionChecker
from deskweave_contracts import (
    AdapterConnectionError,
    AdapterDescriptor,
    AdapterStatus,
    Capability,
    NotImplementedYetError,
    OperationRequest,
    PermissionScope,
    RiskLevel,
)

BLENDER_CAPABILITIES: list[Capability] = [
    Capability(
        name="create_reference_plane",
        description="Create a reference plane in the active Blender scene.",
        parameters_schema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string"},
                "width": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
        risk=RiskLevel.HIGH,
        scope=PermissionScope.BLENDER_CONTROL,
        requires_confirmation=True,
    ),
    Capability(
        name="import_reference_material",
        description="Import a reference image as a material in Blender.",
        parameters_schema={
            "type": "object",
            "properties": {"image_path": {"type": "string"}},
            "required": ["image_path"],
            "additionalProperties": False,
        },
        risk=RiskLevel.HIGH,
        scope=PermissionScope.BLENDER_CONTROL,
        requires_confirmation=True,
    ),
]


class BlenderAdapter(Adapter):
    """Connector to a running Blender instance via the bpy API."""

    def __init__(
        self, *, adapter_id: str = "blender", permission_checker: PermissionChecker | None = None
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Blender",
            version="0.0.1",
            description="Blender connector (bpy-based). Phase 0 skeleton.",
            capabilities=BLENDER_CAPABILITIES,
        )
        super().__init__(descriptor, permission_checker=permission_checker)

    async def connect(self) -> None:
        if find_spec("bpy") is None:
            self._set_status(AdapterStatus.ERROR)
            raise AdapterConnectionError(
                "the bpy module is not importable; the Blender connector is unavailable",
                details={"adapter_id": self.adapter_id, "hint": "run inside Blender's Python"},
            )
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        self._set_status(AdapterStatus.DISCONNECTED)

    async def _run(self, request: OperationRequest) -> dict:
        raise NotImplementedYetError(
            f"blender capability {request.capability!r} is not implemented in Phase 0",
            details={"adapter_id": self.adapter_id, "capability": request.capability},
        )


__all__ = ["BLENDER_CAPABILITIES", "BlenderAdapter"]
