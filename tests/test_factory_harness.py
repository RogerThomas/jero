"""Tests for standalone factory lifecycle: ``BaseFactory.open()`` and ``FactoryHarness``.

``Factory.open()`` is the standalone entry point (async code owns the lifecycle
directly); ``FactoryHarness`` is its sync-test bridge, exercising the *real*
factory — the piece an app's ``factory=`` seam mocks away. These tests assert that
``create_*`` methods build their services and that resources opened on the exit
stacks are closed on exit. The probe factories below open trackable context
managers so teardown is observable without reaching into framework internals.
"""

from collections.abc import Generator
from dataclasses import dataclass
from typing import Self

import pytest

from demo_app import Factory, WidgetService
from demo_app.errors import (
    UpstreamResponseError,
    UpstreamResponseErrorHandler,
    UpstreamUnavailableError,
)
from jero import BaseApp, BaseFactory
from jero.testing import FactoryHarness, TestClient


@pytest.fixture(name="harness")
def _harness(monkeypatch: pytest.MonkeyPatch) -> Generator[FactoryHarness[Factory]]:
    """A FactoryHarness over the demo app's real Factory, torn down after the test.

    The real factory reads settings from the environment, so the env is set here."""
    monkeypatch.setenv("DEMO_APP_ENV", "dev")
    monkeypatch.setenv("DEMO_APP_WIDGET_API_KEY", "api-key")
    monkeypatch.setenv("DEMO_APP_OPENAI_API_KEY", "openai-api-key")
    with FactoryHarness(Factory) as harness:
        yield harness


@dataclass
class AsyncProbe:
    """An async context manager that records whether it was closed."""

    closed: bool = False

    async def __aenter__(self) -> Self:
        """Enter the context, returning self."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the context, recording closure."""
        self.closed = True


@dataclass
class SyncProbe:
    """A sync context manager that records whether it was closed."""

    closed: bool = False

    def __enter__(self) -> Self:
        """Enter the context, returning self."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit the context, recording closure."""
        self.closed = True


class ProbeFactory(BaseFactory):
    """Opens probes on the stacks so the harness's teardown is observable."""

    async def create_async_probe(self) -> AsyncProbe:
        """Open an async probe on the async exit stack."""
        return await self._aenter(AsyncProbe())

    def create_sync_probe(self) -> SyncProbe:
        """Open a sync probe on the sync exit stack."""
        return self._enter(SyncProbe())


class ProbeApp(BaseApp[ProbeFactory]):
    """App whose wire opens probes through its factory."""

    async_probe: AsyncProbe
    sync_probe: SyncProbe

    async def wire(self) -> None:
        self.async_probe = await self._factory.create_async_probe()
        self.sync_probe = self._factory.create_sync_probe()


def test_factory_entered_resources_close_at_app_shutdown() -> None:
    """A resource opened inside a factory create_* shares the app's lifetime: it stays
    open while the app serves and is closed when the app's lifespan ends."""
    app = ProbeApp()
    with TestClient(app):
        assert (app.async_probe.closed, app.sync_probe.closed) == (False, False)
    assert (app.async_probe.closed, app.sync_probe.closed) == (True, True)


def test_harness_runs_async_create_and_closes_on_exit() -> None:
    """An async create_* is awaited via run; its resource is closed on exit."""
    with FactoryHarness(ProbeFactory) as harness:
        probe = harness.run(harness.factory.create_async_probe())
        assert probe.closed is False
    assert probe.closed is True


def test_harness_calls_sync_create_directly_and_closes_on_exit() -> None:
    """A sync create_* is called directly on the factory; its resource is closed on exit."""
    with FactoryHarness(ProbeFactory) as harness:
        probe = harness.factory.create_sync_probe()
        assert probe.closed is False
    assert probe.closed is True


def test_harness_builds_the_real_factory_service(harness: FactoryHarness[Factory]) -> None:
    """The harness builds an actual service from the demo app's factory."""
    service = harness.run(harness.factory.create_widget_service())
    assert isinstance(service, WidgetService)


def test_harness_builds_upstream_response_error_handler(
    harness: FactoryHarness[Factory],
) -> None:
    """The real factory builds the handler from settings."""
    handler = harness.factory.create_upstream_response_error_handler()
    assert isinstance(handler, UpstreamResponseErrorHandler)
    error = harness.run(handler.handle_exception(UpstreamResponseError(retryable=True)))
    assert isinstance(error, UpstreamUnavailableError)
    assert error.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_open_builds_and_closes_standalone() -> None:
    """Factory.open() yields a working factory; everything it opened closes on exit."""
    async with ProbeFactory.open() as factory:
        async_probe = await factory.create_async_probe()
        sync_probe = factory.create_sync_probe()
        states_inside = (async_probe.closed, sync_probe.closed)
    assert states_inside == (False, False)
    assert (async_probe.closed, sync_probe.closed) == (True, True)


async def _open_probe_then_fail(probes: list[AsyncProbe]) -> None:
    """Open a probe inside ProbeFactory.open(), then raise from inside the block."""
    async with ProbeFactory.open() as factory:
        probes.append(await factory.create_async_probe())
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_open_closes_resources_when_the_block_raises() -> None:
    """Factory.open() unwinds the stacks even when the body raises."""
    probes: list[AsyncProbe] = []
    with pytest.raises(RuntimeError, match="boom"):
        await _open_probe_then_fail(probes)
    assert [probe.closed for probe in probes] == [True]
