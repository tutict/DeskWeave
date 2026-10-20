"""Unit tests for Blender discovery and output-path policy (no Blender needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskweave_adapter_blender.runner import (
    RESULT_PREFIX,
    SCENE_PREFIX,
    _extract,
    ensure_output_path,
    find_blender,
)
from deskweave_contracts import AdapterConnectionError


def test_find_blender_prefers_the_explicit_override(tmp_path: Path) -> None:
    fake = tmp_path / "blender.exe"
    fake.write_bytes(b"")
    assert find_blender({"DESKWEAVE_BLENDER": str(fake)}) == fake


def test_find_blender_reports_what_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """When nothing is installed the error must say where we looked."""
    monkeypatch.setattr(
        "deskweave_adapter_blender.runner._candidate_paths",
        lambda env: [Path("/no/blender"), Path("/also/no/blender")],
    )
    with pytest.raises(AdapterConnectionError) as excinfo:
        find_blender({})
    assert "DESKWEAVE_BLENDER" in excinfo.value.message
    assert [Path(entry).name for entry in excinfo.value.details["checked"]] == [
        "blender",
        "blender",
    ]


@pytest.mark.parametrize(
    "name",
    ["Reference Plane", "../../etc/passwd", "a/b/c", "", "..", "ÄÖÜ"],
)
def test_output_path_stays_inside_the_directory(tmp_path: Path, name: str) -> None:
    root = tmp_path / "artifacts"
    target = ensure_output_path(root, "task_1", name)
    assert target.parent == root.resolve()
    assert target.suffix == ".blend"
    assert ".." not in target.name


def test_output_path_is_deterministic_per_task(tmp_path: Path) -> None:
    first = ensure_output_path(tmp_path, "task_abc", "Plane")
    second = ensure_output_path(tmp_path, "task_abc", "Plane")
    assert first == second
    assert first.name == "Plane-task_abc.blend"


def test_output_path_without_a_task(tmp_path: Path) -> None:
    assert ensure_output_path(tmp_path, None, "Plane").name == "Plane-manual.blend"


def test_output_directory_is_created_and_resolved(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "artifacts"
    target = ensure_output_path(root, "task_1", "Plane")
    assert root.is_dir()
    assert target.parent == root.resolve()


def test_extract_returns_the_last_marker_line() -> None:
    output = f'noise\n{RESULT_PREFIX}{{"a": 1}}\nmore noise\n{RESULT_PREFIX}{{"a": 2}}\n'
    assert _extract(output, RESULT_PREFIX) == '{"a": 2}'


def test_extract_returns_none_without_a_marker() -> None:
    assert _extract("just blender noise", SCENE_PREFIX) is None
