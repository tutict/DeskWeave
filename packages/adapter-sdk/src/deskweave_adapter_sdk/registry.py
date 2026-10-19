"""In-memory adapter registry.

The registry owns adapter *discovery*: which connectors exist and how to reach
them. It does not persist anything itself — durable installation metadata lives
in the SQLite ``adapter_installation`` table (see the runtime persistence
module).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from deskweave_contracts import AdapterDescriptor, ConflictError, NotFoundError

from .base import Adapter, PermissionChecker


class AdapterRegistry:
    """Register, look up and unregister adapters by id."""

    def __init__(
        self,
        *,
        permission_checker: PermissionChecker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._permission_checker = permission_checker
        self._logger = logger or logging.getLogger("deskweave.registry")

    def register(self, adapter: Adapter, *, replace: bool = False) -> None:
        """Register ``adapter``.

        Raises :class:`~deskweave_contracts.ConflictError` if an adapter with the
        same id is already registered and ``replace`` is ``False``.
        """
        adapter_id = adapter.adapter_id
        if adapter_id in self._adapters and not replace:
            raise ConflictError(
                f"adapter {adapter_id!r} is already registered",
                details={"adapter_id": adapter_id},
            )
        adapter.set_permission_checker(self._permission_checker)
        self._adapters[adapter_id] = adapter
        self._logger.debug("registered adapter %s", adapter_id)

    def unregister(self, adapter_id: str) -> Adapter:
        """Remove and return the adapter with ``adapter_id``."""
        try:
            adapter = self._adapters.pop(adapter_id)
        except KeyError as exc:
            raise NotFoundError(
                f"adapter {adapter_id!r} is not registered",
                details={"adapter_id": adapter_id},
            ) from exc
        self._logger.debug("unregistered adapter %s", adapter_id)
        return adapter

    def get(self, adapter_id: str) -> Adapter:
        """Return the adapter with ``adapter_id`` or raise ``NotFoundError``."""
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise NotFoundError(
                f"adapter {adapter_id!r} is not registered",
                details={"adapter_id": adapter_id},
            ) from exc

    def list_descriptors(self) -> list[AdapterDescriptor]:
        """Return descriptors for all registered adapters, sorted by id."""
        return [self._adapters[key].descriptor for key in sorted(self._adapters)]

    def list_ids(self) -> list[str]:
        return sorted(self._adapters)

    def set_permission_checker(self, checker: PermissionChecker | None) -> None:
        """Set the permission checker and propagate it to every adapter."""
        self._permission_checker = checker
        for adapter in self._adapters.values():
            adapter.set_permission_checker(checker)

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def __iter__(self) -> Iterator[Adapter]:
        return iter(self._adapters.values())


__all__ = ["AdapterRegistry"]
