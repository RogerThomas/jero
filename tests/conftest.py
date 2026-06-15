"""Shared fixtures for the jero test suite.

Most tests run against the generic ``DemoApp`` with its service layer mocked. The
``client`` fixture passes a ``create_autospec`` stand-in factory into ``DemoApp``
through the public ``factory=`` seam, so ``_wire`` builds the widget resource
against ``widgets_mock`` instead of the real upstream client. Tests configure the
mock's return values and assert how the resource drives it.

Tests that need esoteric wiring (response kinds, scalar-typed sources, streaming,
forms, wiring errors) build their own small apps locally instead.
"""

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from jero import TestClient
from tests.demo_app import DemoApp, Factory, WidgetService


@pytest.fixture(name="widgets_mock")
def _widgets_mock(mocker: MockerFixture) -> MagicMock:
    return mocker.create_autospec(WidgetService, spec_set=True, instance=True)


@pytest.fixture(name="client")
def _client(mocker: MockerFixture, widgets_mock: MagicMock) -> Generator[TestClient]:
    factory = mocker.create_autospec(Factory, spec_set=True, instance=True)
    factory.create_widget_service.return_value = widgets_mock
    with TestClient(DemoApp(factory=factory)) as client:
        yield client
