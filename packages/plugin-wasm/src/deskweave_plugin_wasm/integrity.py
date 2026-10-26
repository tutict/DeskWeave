"""Install-time integrity for WASM plugins: what a hash can and cannot prove.

The threat this module addresses is **content substitution**, not identity:

``modified``
    A developer's own plugin file changed under them — an editor, a bad build,
    a sync client, a half-finished ``cp``.
``swapped``
    Somebody with write access to the plugin directory replaced the module (and
    possibly the manifest) with something else.

A SHA-256 pin detects both of those *if the pin itself is trustworthy*. It is
not a signature: an attacker who can rewrite ``plugin.wasm`` can usually rewrite
the manifest next to it, and the record written by :func:`install` sits in the
same directory by default. This module therefore claims exactly one thing —
**the bytes the host parses are the bytes the pin describes** — and says so
everywhere rather than letting a green check imply "trusted publisher".

What is deliberately *not* here: public keys, signature formats, revocation,
key rotation. Those need a key-distribution story that does not exist yet, and
bolting one on to produce a "verified" badge would be worse than the gap.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .errors import PluginInstallError, PluginIntegrityError, plugin_error
from .manifest import WasmPluginManifest

#: Written next to the manifest after a successful instalment pin.
INSTALL_RECORD_NAME = "deskweave.install.json"
#: Schema version of that record. A future format change gets a new number
#: rather than a best-effort parse of the old one.
RECORD_VERSION = 1
#: Refuse to hash an absurdly large file: hashing 2 GiB to discover it is not a
#: module wastes the one resource an attacker does not have to spend carefully.
MAX_MODULE_BYTES = 64 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, max_bytes: int = MAX_MODULE_BYTES) -> tuple[str, int]:
    """Hash a file streaming, returning ``(digest, size)``.

    Raises :class:`PluginIntegrityError` if the file is larger than ``max_bytes``
    — before reading it into memory.
    """
    file_path = Path(path)
    size = file_path.stat().st_size
    if size > max_bytes:
        raise plugin_error(
            PluginIntegrityError,
            f"plugin module is {size} bytes, above the {max_bytes} byte ceiling",
            path=file_path.name,
            size=size,
            limit=max_bytes,
        )
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest(), size


def is_sha256_hex(value: str) -> bool:
    """Whether ``value`` is 64 lowercase hex characters."""
    return len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def normalise_module_path(raw: str, *, plugin_id: str | None = None) -> str:
    """Validate a manifest-declared module path and return it in POSIX form.

    The rules exist because ``Path(directory) / raw`` is not a containment
    check: ``../../secrets.wasm`` escapes, ``C:\\Windows\\x.wasm`` replaces the
    base entirely, and a bare ``\\\\server\\share`` is a network read. Rejecting
    the *shape* is cheaper and more portable than trying to prove a resolved
    path is safe after the fact.

    Backslashes are treated as separators on every platform so a manifest means
    the same thing on Windows and POSIX.
    """
    if not raw or not raw.strip():
        raise plugin_error(
            PluginIntegrityError,
            "plugin manifest declares an empty module path",
            plugin_id=plugin_id,
        )
    if "\x00" in raw:
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path contains a NUL byte",
            plugin_id=plugin_id,
            path=raw.replace("\x00", "\\0"),
        )

    candidate = raw.replace("\\", "/")
    posix = PurePosixPath(candidate)
    windows = PureWindowsPath(raw)

    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path must be relative to the plugin directory",
            plugin_id=plugin_id,
            path=raw,
        )
    if posix.parts and posix.parts[0] == "..":
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path must not traverse out of the plugin directory",
            plugin_id=plugin_id,
            path=raw,
        )
    if ".." in posix.parts:
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path must not contain '..' components",
            plugin_id=plugin_id,
            path=raw,
        )
    if any(part in {"", "."} for part in candidate.split("/")):
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path contains an empty or '.' component",
            plugin_id=plugin_id,
            path=raw,
        )
    return posix.as_posix()


def _reject_symlink_chain(path: Path, root: Path, *, plugin_id: str | None) -> None:
    """Refuse a module that is, or is reached through, a symbolic link.

    A link inside the directory that points *outside* it defeats every lexical
    check; a link that points inside is still a second name for a file whose
    hash was pinned under a different name. Neither is needed by a plugin
    package, so the rule is simple enough to state in a manual: no links.
    """
    current = path
    while True:
        if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
            raise plugin_error(
                PluginIntegrityError,
                "plugin module path resolves through a symbolic link, which is not allowed",
                plugin_id=plugin_id,
                path=f"{path.name}",
            )
        if current.parent == current or current == root:
            return
        current = current.parent


def resolve_module_path(
    directory: str | Path,
    declared: str,
    *,
    plugin_id: str | None = None,
) -> Path:
    """Resolve ``declared`` inside ``directory``, or raise.

    Checks, in order: lexical shape (see :func:`normalise_module_path`), symlink
    chain, existence, and final containment. Containment is checked last because
    it is the only one that needs the filesystem.
    """
    root = Path(directory).resolve()
    relative = normalise_module_path(declared, plugin_id=plugin_id)
    candidate = root / relative
    _reject_symlink_chain(candidate, root, plugin_id=plugin_id)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise plugin_error(
            PluginIntegrityError,
            "plugin module path resolves outside the plugin directory",
            plugin_id=plugin_id,
            path=relative,
        )
    if not resolved.is_file():
        raise plugin_error(
            PluginIntegrityError,
            "plugin module declared by the manifest does not exist",
            plugin_id=plugin_id,
            path=relative,
        )
    return resolved


@dataclass(frozen=True)
class InstallRecord:
    """The pin written by :func:`install`.

    ``declared_capabilities`` is recorded for *comparison*, not for authority:
    a later load refuses a manifest that has **added** capabilities since the
    instalment, because a plugin does not get to widen its own declaration
    silently. Whether any of them may actually be called is still decided by the
    permission layer at call time.
    """

    plugin_id: str
    version: str
    interface_version: int
    wasm_path: str
    wasm_sha256: str
    size_bytes: int
    declared_capabilities: tuple[str, ...] = ()
    record_version: int = RECORD_VERSION

    @property
    def digest_prefix(self) -> str:
        """Short digest for logs and error messages."""
        return self.wasm_sha256[:12]

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_version": self.record_version,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "interface_version": self.interface_version,
            "wasm_path": self.wasm_path,
            "wasm_sha256": self.wasm_sha256,
            "size_bytes": self.size_bytes,
            "declared_capabilities": list(self.declared_capabilities),
        }

    @classmethod
    def from_dict(cls, raw: Any, *, source: str = INSTALL_RECORD_NAME) -> InstallRecord:
        if not isinstance(raw, dict):
            raise plugin_error(
                PluginIntegrityError, "install record is not a JSON object", path=source
            )
        version = raw.get("record_version")
        if version != RECORD_VERSION:
            raise plugin_error(
                PluginIntegrityError,
                f"install record has unsupported record_version {version!r}",
                path=source,
                expected=RECORD_VERSION,
            )
        digest = raw.get("wasm_sha256")
        if not isinstance(digest, str) or not is_sha256_hex(digest):
            raise plugin_error(
                PluginIntegrityError, "install record has no usable wasm_sha256", path=source
            )
        path = raw.get("wasm_path")
        if not isinstance(path, str):
            raise plugin_error(PluginIntegrityError, "install record has no wasm_path", path=source)
        capabilities = raw.get("declared_capabilities", [])
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise plugin_error(
                PluginIntegrityError,
                "install record has a malformed declared_capabilities list",
                path=source,
            )
        return cls(
            plugin_id=str(raw.get("plugin_id", "")),
            version=str(raw.get("version", "")),
            interface_version=int(raw.get("interface_version", 0)),
            wasm_path=path,
            wasm_sha256=digest,
            size_bytes=int(raw.get("size_bytes", 0)),
            declared_capabilities=tuple(sorted(capabilities)),
            record_version=RECORD_VERSION,
        )


def _write_record(path: Path, record: InstallRecord) -> None:
    """Write the pin atomically: a reader sees the old file or the new one.

    ``os.replace`` is the only step that makes a name visible, so a crash
    halfway through writing can leave a stray temporary file but never a
    truncated record that would parse as "no plugin".
    """
    payload = json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n"
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".install-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _read_record(path: Path) -> InstallRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise plugin_error(
            PluginIntegrityError,
            "install record is unreadable or not valid JSON",
            path=path.name,
        ) from exc
    return InstallRecord.from_dict(raw, source=path.name)


def _manifest_module(manifest: WasmPluginManifest, manifest_path: Path) -> tuple[Path, str]:
    """Return the module path the manifest points at, and its declared name."""
    declared = manifest.wasm_path or f"{manifest_path.stem}.wasm"
    resolved = resolve_module_path(manifest_path.parent, declared, plugin_id=manifest.plugin_id)
    return resolved, normalise_module_path(declared, plugin_id=manifest.plugin_id)


def install(
    manifest_path: str | Path,
    *,
    replace: bool = False,
    record_path: str | Path | None = None,
) -> tuple[str, InstallRecord]:
    """Pin the module a manifest points at.

    Returns ``(outcome, record)`` where outcome is ``"installed"``,
    ``"unchanged"`` or ``"replaced"``. Re-installing the same bytes is
    idempotent; installing *different* bytes over a pin is refused unless
    ``replace`` is set, because the interesting case — "the file changed and I
    did not expect it to" — must not be the default one.
    """
    manifest_file = Path(manifest_path)
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest = WasmPluginManifest.model_validate(raw)
    module_file, declared = _manifest_module(manifest, manifest_file)

    digest, size = sha256_file(module_file)
    if manifest.wasm_sha256 is not None and manifest.wasm_sha256 != digest:
        raise plugin_error(
            PluginInstallError,
            "the manifest's wasm_sha256 does not match the module on disk",
            plugin_id=manifest.plugin_id,
            path=declared,
            declared=manifest.wasm_sha256,
            found=digest,
        )

    record = InstallRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        interface_version=manifest.interface_version,
        wasm_path=declared,
        wasm_sha256=digest,
        size_bytes=size,
        declared_capabilities=tuple(sorted(manifest.declared_capabilities)),
    )

    target = Path(record_path) if record_path else manifest_file.parent / INSTALL_RECORD_NAME
    if target.exists():
        existing = _read_record(target)
        if existing.plugin_id != record.plugin_id:
            raise plugin_error(
                PluginInstallError,
                "the directory is already pinned to a different plugin",
                plugin_id=record.plugin_id,
                path=target.name,
                installed=existing.plugin_id,
                found=record.plugin_id,
            )
        same_bytes = (
            existing.wasm_sha256 == record.wasm_sha256
            and existing.version == record.version
            and existing.declared_capabilities == record.declared_capabilities
            and existing.wasm_path == record.wasm_path
        )
        if same_bytes:
            return "unchanged", existing
        if not replace:
            raise plugin_error(
                PluginInstallError,
                "a different version is already pinned; pass replace=True to upgrade",
                plugin_id=record.plugin_id,
                path=target.name,
                installed={"version": existing.version, "sha256": existing.digest_prefix},
                found={"version": record.version, "sha256": record.digest_prefix},
            )
        _write_record(target, record)
        return "replaced", record

    _write_record(target, record)
    return "installed", record


def verify_install(
    manifest_path: str | Path,
    *,
    record_path: str | Path | None = None,
) -> tuple[InstallRecord, bytes]:
    """Re-check a pinned plugin and return its record and module bytes.

    The bytes returned are the bytes that were hashed, and the caller must parse
    *these* bytes rather than re-reading the file — otherwise the check and the
    use are two different reads, and a file swapped in between would be the one
    that runs.
    """
    manifest_file = Path(manifest_path)
    target = Path(record_path) if record_path else manifest_file.parent / INSTALL_RECORD_NAME
    if not target.is_file():
        raise plugin_error(
            PluginIntegrityError,
            "plugin is not pinned: no install record next to the manifest",
            path=target.name,
        )
    record = _read_record(target)

    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest = WasmPluginManifest.model_validate(raw)
    if manifest.plugin_id != record.plugin_id:
        raise plugin_error(
            PluginIntegrityError,
            "the manifest belongs to a different plugin than the install record",
            plugin_id=manifest.plugin_id,
            path=target.name,
            recorded=record.plugin_id,
        )
    if manifest.version != record.version:
        raise plugin_error(
            PluginIntegrityError,
            "the manifest version does not match the pinned version",
            plugin_id=manifest.plugin_id,
            path=target.name,
            recorded=record.version,
            found=manifest.version,
        )

    module_file, declared = _manifest_module(manifest, manifest_file)
    if declared != record.wasm_path:
        raise plugin_error(
            PluginIntegrityError,
            "the manifest points at a different module than the one that was pinned",
            plugin_id=manifest.plugin_id,
            path=target.name,
            recorded=record.wasm_path,
            found=declared,
        )

    data = module_file.read_bytes()
    if len(data) > MAX_MODULE_BYTES:
        raise plugin_error(
            PluginIntegrityError,
            f"plugin module is {len(data)} bytes, above the {MAX_MODULE_BYTES} byte ceiling",
            plugin_id=manifest.plugin_id,
            path=declared,
        )
    digest = sha256_bytes(data)
    if digest != record.wasm_sha256:
        raise plugin_error(
            PluginIntegrityError,
            "the plugin module changed since it was pinned",
            plugin_id=manifest.plugin_id,
            path=declared,
            expected=record.wasm_sha256,
            found=digest,
        )

    added = manifest.declared_capabilities - set(record.declared_capabilities)
    if added:
        raise plugin_error(
            PluginIntegrityError,
            "the manifest declares capabilities that were not present when it was pinned",
            plugin_id=manifest.plugin_id,
            path=target.name,
            added=sorted(added),
        )
    return record, data


__all__ = [
    "INSTALL_RECORD_NAME",
    "MAX_MODULE_BYTES",
    "RECORD_VERSION",
    "InstallRecord",
    "install",
    "is_sha256_hex",
    "normalise_module_path",
    "resolve_module_path",
    "sha256_bytes",
    "sha256_file",
    "verify_install",
]
