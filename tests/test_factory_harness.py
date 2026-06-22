"""Tests for ``FactoryHarness``: building a factory in isolation, with teardown.

``FactoryHarness`` exercises the *real* factory — the piece an app's ``factory=``
seam mocks away — so these tests assert that ``create_*`` methods build their
services and that resources opened on the exit stacks are closed on harness exit.
The probe factories below open trackable context managers so teardown is
observable without reaching into framework internals.
"""

from collections.abc import Generator
from dataclasses import dataclass
from typing import Self

import pytest

from demo_app import Factory, WidgetService
from jero import BaseFactory, FactoryHarness


@pytest.fixture(name="harness")
def _harness(monkeypatch: pytest.MonkeyPatch) -> Generator[FactoryHarness[Factory]]:
    """A FactoryHarness over the demo app's real Factory, torn down after the test.

    The real factory reads settings from the environment, so the env is set here."""
    monkeypatch.setenv("DEMO_WIDGET_APP_ENV", "dev")
    monkeypatch.setenv("DEMO_WIDGET_APP_API_KEY", "api-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-api-key")
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
