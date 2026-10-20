"""Recovery hooks for adapters whose work leaves a durable file behind.

When the runtime dies between "the target application wrote its output" and "the
runtime recorded the result", the task is left non-terminal and the only honest
question is *did the work happen?*  A file that exists is not evidence: it may be
truncated, empty, or left over from an earlier attempt. An adapter that owns its
output path can answer that question without re-running anything.

The protocol is **optional**. The runtime checks for it while recovering, and
falls back to "ask the user again" when an adapter does not implement it.

Implementations must:

* derive the path from the task id and the request arguments only (never from a
  caller-supplied path), and keep it inside the adapter's own output directory;
* be side-effect free — ``find_output`` must not create or modify anything;
* make ``verify_output`` an *independent* check (re-open the file with the target
  application, not "trust the bytes") and raise
  :class:`~deskweave_contracts.AdapterExecutionError` when the content does not
  match the request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RecoverableOutput(Protocol):
    """An adapter that can re-inspect the output of an interrupted execution."""

    async def find_output(self, *, task_id: str, arguments: dict[str, Any]) -> Path | None:
        """Return the file a previous attempt already produced, or ``None``.

        Called after a restart for a task whose execution was interrupted. A
        return value of ``None`` means "no evidence", not "the work failed".
        """
        ...

    async def verify_output(self, path: Path, *, arguments: dict[str, Any]) -> dict[str, Any]:
        """Re-open ``path`` independently and return a short summary.

        Raises :class:`~deskweave_contracts.AdapterExecutionError` when the file
        does not contain what ``arguments`` asked for.
        """
        ...


__all__ = ["RecoverableOutput"]
