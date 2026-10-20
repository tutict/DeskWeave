"""Hand a produced artifact to the operating system.

The webview never receives a filesystem path. The UI asks for an *artifact id* and
an *action*; the runtime resolves the id through the artifact store — which
re-checks existence and containment inside the allowed roots — and only then
passes the file to the platform.

Two actions exist, because only these two are unambiguous:

``reveal``
    Show the file in the system file manager (selected, where supported).
``open``
    Open the file with the application registered for it. For a ``.blend`` that
    is Blender on any machine where the association exists.

Both are user-initiated and outlive the request, so the launched process is
detached from the runtime: the runtime never waits for Blender to close, and a
failure to *spawn* is reported as :class:`~deskweave_contracts.ArtifactOpenError`
instead of a fake success.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from deskweave_contracts import (
    ArtifactOpenError,
    NotImplementedYetError,
    UnsafeResourceError,
    ValidationError,
)

from .store import ArtifactStore

REVEAL = "reveal"
OPEN = "open"
ACTIONS: frozenset[str] = frozenset({REVEAL, OPEN})


@dataclass(frozen=True)
class LaunchPlan:
    """What will be executed to satisfy an action.

    ``program`` is ``None`` for "let the OS pick the handler" (Windows
    ``os.startfile``), which is the only way to honour the user's file
    associations there.
    """

    action: str
    path: Path
    program: str | None
    arguments: tuple[str, ...] = ()


def plan_launch(action: str, path: Path, platform: str) -> LaunchPlan:
    """Build the launch plan for ``action`` on ``platform``.

    Raises ``NotImplementedYetError`` for a platform with no known opener rather
    than silently doing nothing.
    """
    if platform.startswith("win"):
        if action == REVEAL:
            return LaunchPlan(action, path, "explorer", (f"/select,{path}",))
        return LaunchPlan(action, path, None)
    if platform == "darwin":
        args: tuple[str, ...] = ("-R", str(path)) if action == REVEAL else (str(path),)
        return LaunchPlan(action, path, "open", args)
    if platform.startswith("linux") or platform.startswith("freebsd"):
        target = path.parent if action == REVEAL else path
        return LaunchPlan(action, path, "xdg-open", (str(target),))
    raise NotImplementedYetError(
        f"opening artifacts is not implemented on this platform ({platform})",
        details={"platform": platform},
    )


class ArtifactOpener:
    """Resolves an artifact id and hands the file to the platform."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        platform: str | None = None,
        launch: Callable[[LaunchPlan], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._platform = platform or sys.platform
        self._launch = launch or self._launch_detached
        self._logger = logger or logging.getLogger("deskweave.artifacts.open")

    async def open(self, artifact_id: str, action: str) -> dict[str, Any]:
        """Resolve ``artifact_id`` and perform ``action``. Never returns a path."""
        if action not in ACTIONS:
            raise ValidationError(
                f"unsupported artifact action {action!r}",
                details={"allowed": sorted(ACTIONS)},
            )
        artifact = await self._artifacts.get(artifact_id)
        if artifact.uri is None or not _is_local(artifact.uri):
            raise UnsafeResourceError(
                f"artifact {artifact_id!r} is not a local file and cannot be opened",
                details={"artifact_id": artifact_id, "media_type": artifact.media_type},
            )
        # Re-checks that the file still exists and is inside an allowed root.
        path = await self._artifacts.resolve_path(artifact_id)
        plan = plan_launch(action, path, self._platform)
        self._launch(plan)
        self._logger.info("opened artifact %s (%s) with %s", artifact_id, action, plan.program)
        return {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind.value,
            "media_type": artifact.media_type,
            "action": action,
            "opened": True,
        }

    # -- internals --------------------------------------------------------
    @staticmethod
    def _launch_detached(plan: LaunchPlan) -> None:
        """Start the target process without tying it to the runtime's lifetime."""
        try:
            if plan.program is None:
                # Windows: shell "open" through the user's file associations.
                os.startfile(str(plan.path))  # type: ignore[attr-defined]
                return
            # Fixed program name and a path that the store just verified.
            subprocess.Popen(
                [plan.program, *plan.arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            raise ArtifactOpenError(
                f"could not open the artifact with {plan.program or 'the system'}: {exc}",
                details={"action": plan.action, "program": plan.program},
            ) from exc


def _is_local(uri: str) -> bool:
    """True for ``file:`` URIs and bare paths; false for remote schemes."""
    return urlsplit(uri).scheme in {"", "file"}


__all__ = [
    "ACTIONS",
    "OPEN",
    "REVEAL",
    "ArtifactOpener",
    "LaunchPlan",
    "plan_launch",
]
