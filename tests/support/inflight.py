"""An authority that parks inside ``revoke()``, so a caller can close() mid-flight.

Deterministic by construction: the operation signals ``reached`` from inside the
await it is about to block on and then waits for ``gate``. There is no sleep and
no retry, so the caller either observes the exact interleaving it asked for or
the test fails with a message that says which wait timed out.

It also keeps a small real grant table, because ``approve()`` reconciles the
decision against what the authority says is currently granted: an authority that
always reports "no grants" makes a narrowing approval a no-op, and then it cannot
park.

``mark_closed()`` is called by the test at the moment it closes the lifecycle, so
``completed_after_close`` answers a question no assertion about exceptions can:
did a write into the grants store land *after* the instance stopped being an
authority? That counter is evidence for the report; the load-bearing assertions
in the tests are about the store lock and about files on disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


class GatedAuthority:
    """Capability authority double with a gate in ``revoke``."""

    def __init__(self) -> None:
        #: Set from inside ``revoke`` once the call is parked.
        self.reached = asyncio.Event()
        #: Released by the test to let the parked ``revoke`` finish.
        self.gate = asyncio.Event()
        self.grant_calls = 0
        self.revoke_calls = 0
        #: ``revoke`` calls that ran to completion, and how many of them did so
        #: after the test closed the lifecycle.
        self.completed = 0
        self.completed_after_close = 0
        #: principal -> capabilities, so ``approve()`` can reconcile a narrowing.
        self.granted: dict[str, set[str]] = {}
        #: Raised by ``revoke`` instead of returning, once the gate opens.
        self.fail_with: BaseException | None = None
        self._closed = False

    def mark_closed(self) -> None:
        self._closed = True

    def release(self) -> None:
        self.gate.set()

    async def list_effective_grants(self) -> list[dict[str, Any]]:
        return [
            {"adapter_id": principal, "capability": capability}
            for principal, capabilities in self.granted.items()
            for capability in sorted(capabilities)
        ]

    async def grant(self, request: Any, **kwargs: Any) -> str:
        self.grant_calls += 1
        self.granted.setdefault(str(request.adapter_id), set()).add(str(request.capability))
        return "granted"

    async def revoke(
        self, principal: str, capabilities: Iterable[str] | None = None, **kwargs: Any
    ) -> tuple[str, ...]:
        self.revoke_calls += 1
        self.reached.set()
        await self.gate.wait()
        self.completed += 1
        if self._closed:
            self.completed_after_close += 1
        if self.fail_with is not None:
            raise self.fail_with
        held = self.granted.get(principal, set())
        if capabilities is None:
            removed = sorted(held)
            self.granted.pop(principal, None)
        else:
            removed = sorted(held.intersection(str(c) for c in capabilities))
            self.granted[principal] = held - set(removed)
        return tuple(f"{principal}:{capability}" for capability in removed)
