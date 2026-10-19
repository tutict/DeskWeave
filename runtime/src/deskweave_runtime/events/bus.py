"""In-process event bus.

Phase 0 uses a simple synchronous fan-out to subscribers with an optional
bounded history. No message queue is introduced. Subscribers may be plain
functions or coroutine functions.
"""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import Awaitable, Callable

from deskweave_contracts import Event

Handler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """A minimal publish/subscribe bus with bounded history."""

    def __init__(self, *, history_size: int = 1000) -> None:
        self._handlers: list[Handler] = []
        self._history: deque[Event] = deque(maxlen=history_size)

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """Register ``handler`` and return a callable that unsubscribes it."""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber and record it in history."""
        self._history.append(event)
        for handler in list(self._handlers):
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    def history(self) -> list[Event]:
        """Return the recorded events, oldest first."""
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._handlers)


__all__ = ["EventBus", "Handler"]
