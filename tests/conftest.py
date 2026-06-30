"""Shared fixtures for the jero test suite.

Most tests run against the shared ``demo_app`` with its I/O service layer mocked. The
``client`` fixture passes a ``create_autospec`` stand-in factory into ``DemoApp``
through the public ``factory=`` seam, so ``wire`` builds the widget resource against
``widgets_mock`` instead of the real upstream client. The in-memory ``AnalyticsService``
is *not* mocked — a real one is injected (and exposed as the ``analytics`` fixture) so
its handler keeps a real, type-annotated signature for background registration and tests
can inspect what it processed. Tests configure the mock's return values and assert how
the resource drives it.

Tests that need esoteric wiring (response kinds, scalar-typed sources, streaming,
forms, wiring errors) build their own small apps locally instead.
"""

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from demo_app import AnalyticsService, DemoApp, Factory, WidgetService
from jero import TestClient


@pytest.fixture(name="widgets_mock")
def _widgets_mock(mocker: MockerFixture) -> MagicMock:
    return mocker.create_autospec(WidgetService, spec_set=True, instance=True)


@pytest.fixture(name="analytics_service")
def _analytics_service() -> AnalyticsService:
    return AnalyticsService(processed=[])


@pytest.fixture(name="client")
def _client(
    mocker: MockerFixture, widgets_mock: MagicMock, analytics_service: AnalyticsService
) -> Generator[TestClient]:
    factory = mocker.create_autospec(Factory, spec_set=True, instance=True)
    factory.create_widget_service.return_value = widgets_mock
    factory.create_analytics_service.return_value = analytics_service
    with TestClient(DemoApp(factory=factory)) as client:
        yield client
