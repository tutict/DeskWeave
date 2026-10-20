"""Reference-collection contracts.

These models are the wire format shared by the MCP tools, the control API and the
desktop UI. They describe *requests to collect reference images* and *the
confirmation of a selection* — not a specific application domain, so any workflow
that needs "pick files from the outside world, then act on the chosen ones" can
reuse them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactRef
from .workflow import TaskRecord


class ReferenceSource(StrEnum):
    """A source a reference collection can ask for.

    A source is a *name*, not an implementation: the runtime resolves it to the
    connector that declares it (``Runtime.register_collection_source``). Adding a
    source therefore means adding a connector, not editing the workflow.
    """

    BING = "bing"
    WIKIMEDIA = "wikimedia"


class CollectionRequest(BaseModel):
    """Ask the browser connector to collect candidate reference images."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200, description="Natural-language search query.")
    limit: int = Field(default=6, ge=1, le=20, description="Maximum number of candidates.")
    source: ReferenceSource = Field(
        default=ReferenceSource.BING, description="Search provider to use."
    )


class ConfirmRequest(BaseModel):
    """Confirm a user's selection for a task awaiting confirmation."""

    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Artifacts the user explicitly selected. Must belong to the task.",
    )
    plane_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional name for the created reference plane.",
    )
    target: str = Field(
        default="blender",
        min_length=1,
        max_length=32,
        description="Adapter that should act on the confirmed selection.",
    )


class CollectionFailure(BaseModel):
    """A candidate that could not be turned into an artifact, with the reason."""

    model_config = ConfigDict(extra="forbid")

    url: str
    reason: str


class ReferenceTaskView(BaseModel):
    """A task together with the artifacts it produced.

    This is the read model shared by the control API and the desktop UI. It
    deliberately contains no filesystem paths: artifacts are addressed by id and
    their bytes are served through a dedicated preview endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    task: TaskRecord
    artifacts: list[ArtifactRef] = Field(default_factory=list)


__all__ = [
    "CollectionFailure",
    "CollectionRequest",
    "ConfirmRequest",
    "ReferenceSource",
    "ReferenceTaskView",
]
