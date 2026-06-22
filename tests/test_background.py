"""Background tasks: type-inferred registration, dispatch, drain, and error isolation.

The async behaviour is exercised directly through ``BackgroundTasks``'s public interface
(it is an async context manager); one end-to-end test drives it wired into an app through
the ``TestClient``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from msgspec import Struct
from pytest_mock import MockerFixture

from demo_app import AnalyticsService, DemoApp, Factory, WidgetService
from demo_app.models import Widget
from jero import BackgroundTasks, TestClient, WiringError


class Event(Struct):
    """Test event type."""

    name: str


class Other(Struct):
    """Alternative test struct type."""

    value: int


_BgHandler = Callable[[Event], Awaitable[None]]


@pytest.mark.asyncio
async def test_dispatches_to_registered_handler() -> None:
    """Handler receives items dispatched to background tasks."""
    seen: list[str] = []

    async def handle(item: Event) -> None:
        seen.append(item.name)

    async with BackgroundTasks(drain_timeout=1.0) as tasks:
        tasks.register(handle)
        await tasks.add(Event(name="event-name"))
    # leaving the context drains the queue, so the item has been processed
    assert seen == ["event-name"]


@pytest.mark.asyncio
async def test_dispatch_is_by_item_type() -> None:
    """Different handlers are called based on item type."""
    events: list[str] = []
    others: list[int] = []

    async def on_event(item: Event) -> None:
        events.append(item.name)

    async def on_other(item: Other) -> None:
        others.append(item.value)

    async with BackgroundTasks(drain_timeout=1.0) as tasks:
        tasks.register(on_event)
        tasks.register(on_other)
        await tasks.add(Other(value=1))
        await tasks.add(Event(name="event-name"))
    assert events == ["event-name"]
    assert others == [1]


def test_duplicate_registration_for_a_type_raises() -> None:
    """Registering multiple handlers for the same type raises an error."""

    async def first(_item: Event) -> None: ...

    async def second(_item: Event) -> None: ...

    tasks = BackgroundTasks()
    tasks.register(first)
    with pytest.raises(WiringError, match="already registered"):
        tasks.register(second)


@pytest.mark.asyncio
async def test_allow_one_to_many_runs_every_handler() -> None:
    """With allow_one_to_many, all handlers for the same type are called."""
    seen: list[str] = []

    async def first(_item: Event) -> None:
        seen.append("first")

    async def second(_item: Event) -> None:
        seen.append("second")

    async with BackgroundTasks(drain_timeout=1.0, allow_one_to_many=True) as tasks:
        tasks.register(first)
        tasks.register(second)
        await tasks.add(Event(name="event-name"))
    assert seen == ["first", "second"]


async def _no_args() -> None: ...


async def _two_args(_a: Event, _b: Event) -> None: ...


async def _not_a_struct(_item: int) -> None: ...


@pytest.mark.parametrize("handler", [_no_args, _two_args])
def test_register_rejects_wrong_arity(handler: object) -> None:
    """Handlers must have exactly one argument."""
    with pytest.raises(WiringError, match="exactly one argument"):
        BackgroundTasks().register(cast(_BgHandler, handler))


def test_register_rejects_non_struct_param() -> None:
    """Handler parameter must be a Struct subclass."""
    with pytest.raises(WiringError, match="Struct subclass"):
        BackgroundTasks().register(cast(_BgHandler, _not_a_struct))


def test_register_rejects_unannotated_param() -> None:
    """Handler parameter must be type-annotated."""

    async def handler(_item: Event) -> None: ...

    del handler.__annotations__["_item"]  # simulate a handler whose param isn't annotated
    with pytest.raises(WiringError, match="Struct subclass"):
        BackgroundTasks().register(handler)


@pytest.mark.asyncio
async def test_a_failing_handler_does_not_kill_the_worker() -> None:
    """Worker continues processing after a handler raises an exception."""
    seen: list[int] = []

    async def boom(_item: Event) -> None:
        raise RuntimeError("boom")

    async def ok(item: Other) -> None:
        seen.append(item.value)

    async with BackgroundTasks(drain_timeout=1.0) as tasks:
        tasks.register(boom)
        tasks.register(ok)
        await tasks.add(Event(name="event-name"))
        await tasks.add(Other(value=1))
    assert seen == [1]  # the worker survived the raising handler


@pytest.mark.asyncio
async def test_drain_timeout_drops_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Drain timeout drops pending items and logs a warning."""
    seen: list[str] = []

    async def slow(item: Event) -> None:
        await asyncio.sleep(5)
        seen.append(item.name)

    with caplog.at_level(logging.WARNING, logger="jero.background"):
        async with BackgroundTasks(drain_timeout=0.05) as tasks:
            tasks.register(slow)
            await tasks.add(Event(name="event-name"))
    assert not seen
    assert "drain timed out" in caplog.text


@pytest.mark.asyncio
async def test_drain_timeout_none_does_not_wait() -> None:
    """With drain_timeout=None, pending items are not processed."""
    seen: list[str] = []

    async def slow(item: Event) -> None:
        await asyncio.sleep(5)
        seen.append(item.name)

    async with BackgroundTasks(drain_timeout=None) as tasks:
        tasks.register(slow)
        await tasks.add(Event(name="event-name"))
    assert not seen


@pytest.mark.asyncio
async def test_unknown_item_type_is_logged_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    """Unknown item types are logged but do not crash the worker."""
    with caplog.at_level(logging.ERROR, logger="jero.background"):
        async with BackgroundTasks(drain_timeout=1.0) as tasks:
            await tasks.add(Event(name="event-name"))  # nothing registered for Event
    assert "no handler registered" in caplog.text


def test_end_to_end_processes_through_the_app(mocker: MockerFixture) -> None:
    """Creating a widget records an analytics event, drained when the app context closes."""
    analytics_service = AnalyticsService(processed=[])
    widgets_mock = mocker.create_autospec(WidgetService, spec_set=True, instance=True)
    widgets_mock.create_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    factory = mocker.create_autospec(Factory, spec_set=True, instance=True)
    factory.create_widget_service.return_value = widgets_mock
    factory.create_analytics_service.return_value = analytics_service
    with TestClient(DemoApp(factory=factory)) as client:
        resp = client.post(
            "/widgets",
            json={"name": "name", "priceCents": 1},
            headers={"authorization": "Bearer token"},
        )
        assert resp.status_code == 201
    # the client context closed the lifespan, draining the queue
    assert analytics_service.processed == ["created:widget-id"]
