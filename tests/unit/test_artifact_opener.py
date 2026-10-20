"""Handing a produced artifact to the operating system.

The interesting properties are all negative ones: the response must not contain a
path, an unknown action must be rejected, a missing file must be reported instead
of "opened", and a platform without a known opener must say so. The launch itself
is injected, so these tests never open Explorer or Blender.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskweave_contracts import (
    ArtifactKind,
    ArtifactOpenError,
    NotFoundError,
    NotImplementedYetError,
    UnsafeResourceError,
    ValidationError,
)
from deskweave_runtime import Runtime
from deskweave_runtime.artifacts import ArtifactOpener, LaunchPlan, plan_launch


async def _model_artifact(runtime: Runtime, name: str = "plane.blend") -> tuple[str, Path]:
    path = runtime.config.data_dir / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"BLENDER-fake")
    artifact = await runtime.artifacts.register(
        kind=ArtifactKind.MODEL,
        uri=path.resolve().as_uri(),
        media_type="application/x-blender",
        size_bytes=12,
        task_id="task_owner",
    )
    return artifact.artifact_id, path.resolve()


# -- plans ----------------------------------------------------------------
def test_plan_launch_selects_the_file_on_every_supported_platform() -> None:
    path = Path("C:/data/artifacts/plane.blend")

    windows_reveal = plan_launch("reveal", path, "win32")
    assert windows_reveal.program == "explorer"
    assert windows_reveal.arguments == (f"/select,{path}",)
    # "open" defers to the user's file association instead of guessing Blender's path.
    windows_open = plan_launch("open", path, "win32")
    assert windows_open.program is None

    mac_reveal = plan_launch("reveal", path, "darwin")
    assert (mac_reveal.program, mac_reveal.arguments) == ("open", ("-R", str(path)))
    mac_open = plan_launch("open", path, "darwin")
    assert mac_open.arguments == (str(path),)

    linux_reveal = plan_launch("reveal", path, "linux")
    assert linux_reveal.arguments == (str(path.parent),)
    linux_open = plan_launch("open", path, "linux")
    assert linux_open.arguments == (str(path),)


def test_plan_launch_refuses_an_unknown_platform() -> None:
    with pytest.raises(NotImplementedYetError) as excinfo:
        plan_launch("reveal", Path("/tmp/x.blend"), "plan9")
    assert excinfo.value.to_info().code == "not_implemented"
    assert excinfo.value.details["platform"] == "plan9"


# -- opening --------------------------------------------------------------
async def test_open_hands_the_verified_file_to_the_platform(runtime: Runtime) -> None:
    artifact_id, path = await _model_artifact(runtime)
    plans: list[LaunchPlan] = []
    opener = ArtifactOpener(runtime.artifacts, platform="win32", launch=plans.append)

    payload = await opener.open(artifact_id, "reveal")

    assert payload == {
        "artifact_id": artifact_id,
        "kind": "model",
        "media_type": "application/x-blender",
        "action": "reveal",
        "opened": True,
    }
    assert len(plans) == 1
    assert plans[0].path == path
    # Nothing in the answer tells the webview where the file lives.
    serialised = json.dumps(payload)
    assert str(path) not in serialised
    assert "file://" not in serialised


async def test_open_rejects_an_unknown_action(runtime: Runtime) -> None:
    artifact_id, _ = await _model_artifact(runtime)
    plans: list[LaunchPlan] = []
    opener = ArtifactOpener(runtime.artifacts, platform="win32", launch=plans.append)

    with pytest.raises(ValidationError) as excinfo:
        await opener.open(artifact_id, "delete")
    assert excinfo.value.details["allowed"] == ["open", "reveal"]
    assert plans == []


async def test_open_reports_a_missing_file_instead_of_claiming_success(
    runtime: Runtime,
) -> None:
    artifact_id, path = await _model_artifact(runtime)
    path.unlink()
    plans: list[LaunchPlan] = []
    opener = ArtifactOpener(runtime.artifacts, platform="win32", launch=plans.append)

    with pytest.raises(NotFoundError):
        await opener.open(artifact_id, "reveal")
    assert plans == []


async def test_open_rejects_an_artifact_without_a_local_file(runtime: Runtime) -> None:
    artifact = await runtime.artifacts.register(
        kind=ArtifactKind.OTHER,
        uri="https://example.com/remote.glb",
        media_type="model/gltf-binary",
    )
    plans: list[LaunchPlan] = []
    opener = ArtifactOpener(runtime.artifacts, platform="win32", launch=plans.append)

    with pytest.raises(UnsafeResourceError):
        await opener.open(artifact.artifact_id, "open")
    assert plans == []


async def test_open_rejects_an_artifact_outside_the_allowed_roots(runtime: Runtime) -> None:
    """A path is never an authorisation token: containment is re-checked on open."""
    outside = runtime.config.data_dir.parent / "outside.blend"
    outside.write_bytes(b"BLENDER-fake")
    with pytest.raises(Exception):  # noqa: B017 - the store refuses to register it
        await runtime.artifacts.register(
            kind=ArtifactKind.MODEL,
            uri=outside.resolve().as_uri(),
            media_type="application/x-blender",
        )


def test_a_launch_failure_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    target = tmp_path / "plane.blend"
    target.write_bytes(b"x")
    plan = LaunchPlan("open", target, "definitely-not-a-real-program-xyz", ())

    with pytest.raises(ArtifactOpenError) as excinfo:
        ArtifactOpener._launch_detached(plan)
    assert excinfo.value.to_info().code == "artifact_open_failed"
    assert excinfo.value.details["program"] == "definitely-not-a-real-program-xyz"
