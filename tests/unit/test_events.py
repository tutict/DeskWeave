"""Unit tests for the in-process event bus."""

from __future__ import annotations

from deskweave_contracts import Event, EventType
from deskweave_runtime.events import EventBus


async def test_publish_calls_sync_and_async_handlers() -> None:
    bus = EventBus()
    seen: list[str] = []

    def sync_handler(event: Event) -> None:
        seen.append(f"sync:{event.type.value}")

    async def async_handler(event: Event) -> None:
        seen.append(f"async:{event.type.value}")

    bus.subscribe(sync_handler)
    bus.subscribe(async_handler)
    await bus.publish(Event(type=EventType.RUNTIME_STARTED))
    assert seen == ["sync:runtime.started", "async:runtime.started"]


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[int] = []
    unsubscribe = bus.subscribe(lambda event: seen.append(1))
    await bus.publish(Event(type=EventType.RUNTIME_STARTED))
    unsubscribe()
    await bus.publish(Event(type=EventType.RUNTIME_STOPPED))
    assert seen == [1]


async def test_history_is_recorded() -> None:
    bus = EventBus()
    await bus.publish(Event(type=EventType.RUNTIME_STARTED))
    assert len(bus.history()) == 1
    bus.clear()
    assert bus.history() == []
