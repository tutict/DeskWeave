"""Read-only filesystem connector.

Unlike the browser and Blender skeletons, this connector is functional: it can
list directories and read text files, but only inside explicitly allowed roots.
Any access outside those roots is denied by the connector itself (in addition to
the runtime permission layer), demonstrating the allowed-root constraint.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from deskweave_adapter_sdk import Adapter, PermissionChecker
from deskweave_contracts import (
    AdapterDescriptor,
    AdapterStatus,
    Capability,
    NotImplementedYetError,
    OperationRequest,
    PermissionDeniedError,
    PermissionScope,
    RiskLevel,
)

MAX_READ_BYTES = 1_000_000

FILESYSTEM_CAPABILITIES: list[Capability] = [
    Capability(
        name="list_dir",
        description="List the entries of a directory inside an allowed root.",
        parameters_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
        scope=PermissionScope.FILESYSTEM_READ,
    ),
    Capability(
        name="read_text",
        description="Read a UTF-8 text file inside an allowed root.",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
        scope=PermissionScope.FILESYSTEM_READ,
    ),
]


class FilesystemAdapter(Adapter):
    """Read-only access to files under one or more allowed roots."""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str | Path],
        adapter_id: str = "filesystem",
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        descriptor = AdapterDescriptor(
            adapter_id=adapter_id,
            name="Filesystem",
            version="0.0.1",
            description="Read-only filesystem connector with allowed-root enforcement.",
            capabilities=FILESYSTEM_CAPABILITIES,
        )
        super().__init__(descriptor, permission_checker=permission_checker)
        self._roots = tuple(Path(root).resolve() for root in allowed_roots)

    async def connect(self) -> None:
        self._set_status(AdapterStatus.CONNECTED)

    async def get_capabilities(self) -> list[Capability]:
        return self.descriptor.capabilities

    async def disconnect(self) -> None:
        self._set_status(AdapterStatus.DISCONNECTED)

    async def _run(self, request: OperationRequest) -> dict:
        path = self._resolve(request.arguments["path"])
        if request.capability == "list_dir":
            if not path.is_dir():
                raise PermissionDeniedError(
                    f"{path} is not a directory", details={"path": str(path)}
                )
            entries = sorted(child.name for child in path.iterdir())
            return {"path": str(path), "entries": entries}
        if request.capability == "read_text":
            max_bytes = int(request.arguments.get("max_bytes", MAX_READ_BYTES))
            data = path.read_bytes()[:max_bytes]
            return {"path": str(path), "text": data.decode("utf-8", errors="replace")}
        raise NotImplementedYetError(
            f"filesystem capability {request.capability!r} is not implemented",
            details={"adapter_id": self.adapter_id, "capability": request.capability},
        )

    def _resolve(self, raw_path: str) -> Path:
        if not self._roots:
            raise PermissionDeniedError(
                "filesystem adapter has no allowed roots configured",
                details={"path": raw_path},
            )
        resolved = Path(raw_path).resolve()
        for root in self._roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise PermissionDeniedError(
            "path is outside the allowed roots",
            details={"path": str(resolved), "allowed_roots": [str(r) for r in self._roots]},
        )


__all__ = ["FILESYSTEM_CAPABILITIES", "MAX_READ_BYTES", "FilesystemAdapter"]
