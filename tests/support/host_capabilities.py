"""Real host capabilities used to test execution governance.

These are not mocks. Each one either blocks a thread, awaits on the event loop, or
refuses to die, and each records what happened to *it* — start, finish, whether it
saw a cancellation — so a test can assert on the capability's own view instead of
trusting the caller's report.

The distinction the whole phase turns on: a capability that blocks cannot be
cancelled, and a capability that swallows ``CancelledError`` must not be reported
as cancelled just because somebody asked.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, ClassVar

from deskweave_contracts import PermissionScope, RiskLevel

PLUGIN_CALL = PermissionScope.PLUGIN_HOST_CALL


class BlockingCapability:
    """Synchronous and slow. Bounded pool, timeout only, never cancellable."""

    scope = PLUGIN_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    supports_cancellation = False

    def __init__(
        self,
        name: str = "test.blocking",
        *,
        delay: float = 0.25,
        cost: int = 1,
        rate_limit_per_s: float | None = None,
    ) -> None:
        self.name = name
        self.delay = delay
        self.cost = cost
        self.rate_limit_per_s = rate_limit_per_s
        self.runs = 0
        self.running = 0
        self.max_running = 0
        self.finished_at: float | None = None
        self._lock = threading.Lock()

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.runs += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.running -= 1
            self.finished_at = time.perf_counter()
        return {"slept": self.delay, "runs": self.runs}


class AsyncCapability:
    """A coroutine capability. Cancellable, and it proves it by recording the fact."""

    scope = PLUGIN_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    supports_cancellation = True

    def __init__(
        self,
        name: str = "test.async",
        *,
        delay: float = 0.25,
        cost: int = 1,
        rate_limit_per_s: float | None = None,
    ) -> None:
        self.name = name
        self.delay = delay
        self.cost = cost
        self.rate_limit_per_s = rate_limit_per_s
        self.runs = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancelled_at: float | None = None

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs += 1
        self.started_at = time.perf_counter()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled_at = time.perf_counter()
            raise
        self.finished_at = time.perf_counter()
        return {"waited": self.delay}


class StubbornAsyncCapability:
    """Catches ``CancelledError`` and keeps working.

    Exists because "we asked" and "it stopped" are different facts, and an
    implementation that treats them as the same passes every other test in this
    file.
    """

    scope = PLUGIN_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    supports_cancellation = True

    def __init__(
        self, name: str = "test.stubborn", *, delay: float = 0.6, extra: float = 0.4
    ) -> None:
        self.name = name
        self.delay = delay
        self.extra = extra
        self.cost = 1
        self.saw_cancel = False
        self.finished_at: float | None = None

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.saw_cancel = True
            # Deliberately swallow it: this is what a badly written capability
            # wrapping a thread does, and the executor must not pretend the
            # abort worked.
            await asyncio.sleep(self.extra)
        self.finished_at = time.perf_counter()
        return {"stubborn": True}


class RaisingCapability:
    """Fails after it was admitted, so the audit must show admitted-but-failed."""

    name = "test.raises"
    scope = PLUGIN_CALL
    risk = RiskLevel.LOW
    parameters_schema: ClassVar[dict[str, Any]] = {"type": "object"}
    cost = 1

    def __init__(self) -> None:
        self.runs = 0

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs += 1
        raise RuntimeError("capability exploded with token=sk-live-secret")
