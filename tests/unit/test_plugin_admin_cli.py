"""The read-only admin tool: what it reports, and what it refuses to be.

The interesting assertion here is the last one — the CLI has no verb that grants,
revokes or activates anything. A read-only tool cannot become an authorisation
bypass by accident, and a test that pins the absence is cheaper than a review that
has to remember to check.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from deskweave_plugin_wasm.integrity import install
from support import wasm_plugins as fixtures

# `scripts/` is not a package, so load the CLI by path rather than importing it.
# That also keeps the test honest: it exercises the file that ships, not a copy.
_SPEC = importlib.util.spec_from_file_location(
    "plugin_admin", fixtures.REPO_ROOT / "scripts" / "plugin_admin.py"
)
assert _SPEC is not None and _SPEC.loader is not None
plugin_admin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(plugin_admin)


def _copy_example(tmp_path: Path) -> Path:
    target = tmp_path / "reference-metadata"
    shutil.copytree(fixtures.EXAMPLE_DIR, target)
    return target


def test_list_reports_the_example_plugin(tmp_path: Path, capsys) -> None:
    code = plugin_admin.main(["list", str(fixtures.EXAMPLE_DIR.parent)])
    out = capsys.readouterr().out
    assert code == 0
    assert "reference-metadata" in out
    assert "0.1.0" in out
    assert "reference.metadata.validate" in out


def test_inspect_reports_an_unpinned_plugin_honestly(capsys) -> None:
    code = plugin_admin.main(
        ["inspect", "reference-metadata", "--plugins-dir", str(fixtures.EXAMPLE_DIR.parent)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "no install record" in out
    # No store was given, so approval state is *unknown*, never "none".
    assert "unknown (no approval store given)" in out


def test_inspect_exits_three_when_the_module_drifted(tmp_path: Path, capsys) -> None:
    plugin_dir = _copy_example(tmp_path)
    install(plugin_dir / "reference_metadata.json")
    (plugin_dir / "reference_metadata.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")

    code = plugin_admin.main(["inspect", "reference-metadata", "--plugins-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 3
    assert "MODIFIED SINCE INSTALL" in out


def test_inspect_reports_a_missing_plugin_as_not_found(tmp_path: Path) -> None:
    assert plugin_admin.main(["inspect", "ghost", "--plugins-dir", str(tmp_path)]) == 2


@pytest.mark.parametrize("verb", ["approve", "revoke", "activate", "uninstall", "install"])
def test_the_cli_has_no_state_changing_verbs(verb: str) -> None:
    with pytest.raises(SystemExit):
        plugin_admin.main([verb, "reference-metadata"])
