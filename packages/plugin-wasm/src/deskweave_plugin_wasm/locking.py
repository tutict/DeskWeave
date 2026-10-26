"""One governing process per approval store.

The approval store is a plain JSON file with an atomic replace and no lock, so
two processes can both read it, both decide, and both write: one decision is
lost, and — worse — each process has its own revocation barrier, so a call that
process A already queued is admitted after process B revokes (Phase 2.6.4 F, the
single-process constraint). Documentation cannot prevent that. An exclusive lock
can, for every process that cooperates with it.

Design decisions, stated rather than implied:

* **The lock file is a separate, stable file next to the store** — ``<name>.lock``
  — not the store itself. The store is replaced by ``os.replace`` on every save,
  so a lock held on its old inode would stop meaning anything after the first
  write.
* **The file is never deleted on release.** Deleting it and creating a new one
  lets two processes lock two different inodes under one name, which is a lock
  that reports success and excludes nobody.
* **One OS lock per process, not per object.** Two lifecycles in one process over
  one store are a supported configuration (different surfaces, one decision
  record); they share the process's lock through a reference count, so the second
  one does not deadlock against the first, and the OS lock drops only when the
  last holder releases.
* **Windows and POSIX each get their native primitive** (``msvcrt.locking`` and
  ``fcntl.flock``, both non-blocking). There is no no-op fallback: a platform
  without either is refused, because a silent no-op lock is exactly the
  "documented but unenforced" state this module exists to end.
* **Release is explicit** (``release`` / ``PluginLifecycle.close``); a
  ``weakref.finalize`` backstop exists so a dropped object does not pin the lock
  forever, but it is not the supported path and ``__del__`` is not used.
* **"Busy" and "broken" are different answers.** ``PluginStoreLockedError`` means
  another process holds the store, which a caller may retry; every other failure
  (no platform primitive, an unwritable directory, a lock file that cannot be
  opened) is ``PluginLifecycleError``, because retrying it will not help and
  reporting it as a conflict would send the caller looking for a process that
  does not exist (Phase 2.6.6: the create/open failure used to be reported as a
  conflict).
* **``acquire`` creates the store's parent directory** — ``mkdir(parents=True,
  exist_ok=True)`` — because a lifecycle may legitimately be constructed before
  the first save, and the save itself creates that directory. It is the only
  write this module performs, it is idempotent, and it is part of the contract
  (tested): everything else here only touches ``<store>.lock``.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import PluginLifecycleError, PluginStoreLockedError, plugin_error

#: Suffix of the lock file. Kept a constant so a test can find it without
#: repeating the rule, and so the name never depends on the store being healthy.
LOCK_SUFFIX = ".lock"

_GUARD = threading.Lock()
_HELD: dict[str, _Held] = {}

#: errno values that mean "someone else has it" rather than "it is broken".
_BUSY = tuple(
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)


@dataclass
class _Held:
    fd: int
    lock_path: Path
    count: int
    backend: str


class StoreLock:
    """A handle on the process's lock for one store. Releasing twice is a no-op."""

    __slots__ = ("_released", "key", "lock_path")

    def __init__(self, key: str, lock_path: Path) -> None:
        self.key = key
        self.lock_path = lock_path
        self._released = False

    @property
    def released(self) -> bool:
        return self._released


def lock_path_for(store_path: str | Path) -> Path:
    """``<store>.lock`` — derived from the *resolved* store path, never the cwd.

    The resolution happens here rather than being left to the caller: a relative
    path would otherwise produce a lock file, and a registry key, that depend on
    the process's working directory, so one store reached two ways would be two
    locks — the "reports success and excludes nobody" failure this module exists
    to prevent.
    """
    path = Path(store_path).resolve()
    return path.with_name(path.name + LOCK_SUFFIX)


def acquire(store_path: str | Path) -> StoreLock:
    """Take the process-wide exclusive lock for ``store_path``, or refuse.

    Raises :class:`PluginStoreLockedError` when another process holds it, and
    :class:`PluginLifecycleError` when a lock cannot be established at all (no
    platform primitive, unwritable directory). Neither case degrades to running
    unlocked.
    """
    try:
        path = Path(store_path).resolve()
    except (OSError, ValueError) as exc:
        raise plugin_error(
            PluginLifecycleError,
            "the approval store's path cannot be resolved, so no lock can be attributed "
            "to it; refusing to run unlocked",
            path=str(store_path),
        ) from exc
    key = os.path.normcase(str(path))
    lock_path = lock_path_for(path)

    with _GUARD:
        held = _HELD.get(key)
        if held is not None:
            held.count += 1
            return StoreLock(key, lock_path)

        fd = _open_lock_file(lock_path, path)
        backend = _lock(fd, lock_path, path)
        _HELD[key] = _Held(fd=fd, lock_path=lock_path, count=1, backend=backend)
        return StoreLock(key, lock_path)


def release(lock: StoreLock) -> None:
    """Drop one reference; the OS lock goes when the last one does."""
    if lock._released:
        return
    lock._released = True
    with _GUARD:
        held = _HELD.get(lock.key)
        if held is None:  # pragma: no cover - only if the registry was cleared
            return
        held.count -= 1
        if held.count > 0:
            return
        del _HELD[lock.key]
        try:
            _unlock(held.fd, held.backend)
        except OSError:  # closing the descriptor releases it anyway
            pass
        finally:
            os.close(held.fd)


def held_keys() -> frozenset[str]:
    """Canonical store keys this process currently holds. For tests and diagnostics."""
    with _GUARD:
        return frozenset(_HELD)


def _open_lock_file(lock_path: Path, store_path: Path) -> int:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # Not a conflict: nobody else is holding anything, this process simply
        # cannot create the file (a read-only directory, a name that is a
        # directory, a full disk). Reporting it as ``plugin_store_locked`` would
        # tell the caller to wait for a process that does not exist.
        raise _broken(store_path, lock_path, exc) from exc


def _lock(fd: int, lock_path: Path, store_path: Path) -> str:
    """Take a non-blocking exclusive lock. Returns the backend used."""
    if sys.platform == "win32":
        import msvcrt

        # `msvcrt.locking` locks from the current position, and refuses to lock
        # past the end of an empty file on some systems: give it one byte.
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return "msvcrt"
        except OSError as exc:
            os.close(fd)
            if exc.errno in _BUSY:
                raise _busy(store_path, lock_path, exc) from exc
            raise _broken(store_path, lock_path, exc) from exc

    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - no supported platform lacks both
        os.close(fd)
        raise plugin_error(
            PluginLifecycleError,
            "this platform has neither msvcrt nor fcntl, so this host cannot take an "
            "exclusive lock on the approval store and will not run unlocked",
            path=str(store_path),
            lock_path=str(lock_path),
        ) from exc

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "flock"
    except OSError as exc:
        os.close(fd)
        if exc.errno in _BUSY:
            raise _busy(store_path, lock_path, exc) from exc
        raise _broken(store_path, lock_path, exc) from exc


def _unlock(fd: int, backend: str) -> None:
    if backend == "msvcrt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _busy(store_path: Path, lock_path: Path, exc: OSError) -> Exception:
    return plugin_error(
        PluginStoreLockedError,
        "another process already holds this approval store; one store may be governed "
        "by only one process at a time",
        path=str(store_path),
        lock_path=str(lock_path),
        problem=str(exc),
    )


def _broken(store_path: Path, lock_path: Path, exc: OSError) -> Exception:
    return plugin_error(
        PluginLifecycleError,
        "the approval store's lock could not be taken; refusing to run unlocked",
        path=str(store_path),
        lock_path=str(lock_path),
        problem=str(exc),
    )


__all__ = ["LOCK_SUFFIX", "StoreLock", "acquire", "held_keys", "lock_path_for", "release"]
