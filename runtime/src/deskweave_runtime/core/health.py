"""Health model.

The runtime reports *real* health derived from live component state (database
reachable, adapters connected, ...). It never fabricates a healthy status.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPED = "stopped"


class ComponentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: HealthState
    detail: str = ""


class AdapterHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    status: str
    capabilities: list[str] = Field(default_factory=list)


class RuntimeHealth(BaseModel):
    """Aggregate health of the runtime, safe to serialise to the UI/MCP."""

    model_config = ConfigDict(extra="forbid")

    state: HealthState
    version: str
    uptime_seconds: float
    schema_version: int
    database_path: str
    components: list[ComponentHealth] = Field(default_factory=list)
    adapters: list[AdapterHealth] = Field(default_factory=list)


__all__ = ["AdapterHealth", "ComponentHealth", "HealthState", "RuntimeHealth"]
