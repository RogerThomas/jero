"""Annotations jero reads at wiring must resolve at runtime.

A type imported only under ``if TYPE_CHECKING:`` (what ruff's TC rules suggest for a name
used only in annotations) is invisible when jero evaluates the annotation at startup. Every
wiring site reports it as a ``WiringError`` naming the class and method, never a bare
``NameError``. The annotations are quoted so the module itself imports on 3.13, where
unquoted annotations are evaluated at definition.
"""

from typing import TYPE_CHECKING

import pytest

from jero import (
    BackgroundTasks,
    BaseApp,
    Endpoint,
    WebSocket,
    WebSocketEndpoint,
    WiringError,
)
from jero.testing import TestClient

if TYPE_CHECKING:
    from demo_app.models import Credentials, Health, WidgetPath
    from jero import ExceptionResponse, NotFoundError, Request


class HealthEndpoint(Endpoint, path="/health"):
    """A route for the auth and middleware cases to cover."""

    def get(self) -> "Health":
        """Never called: wiring fails first."""
        raise NotImplementedError


class ParamsEndpoint(Endpoint, path="/params"):
    """A handler whose source type is a TYPE_CHECKING-only import."""

    def get(self, params: "WidgetPath") -> "Health":
        """Never called: wiring fails first."""
        raise NotImplementedError


class HeadersAuth:
    """An authenticator whose headers type is a TYPE_CHECKING-only import."""

    def authenticate(self, headers: "Credentials") -> str:
        """Never called: wiring fails first."""
        raise NotImplementedError


class ParamsWebSocket(WebSocketEndpoint, path="/socket"):
    """A WebSocket handler whose params type is a TYPE_CHECKING-only import."""

    async def handle(self, websocket: WebSocket[str, str], params: "WidgetPath") -> None:
        """Never called: wiring fails first."""
        raise NotImplementedError


class RequestObserver:
    """An observe hook whose request headers type is a TYPE_CHECKING-only import."""

    def observe(self, request: "Request[Credentials]", status: int, duration: float) -> None:
        """Never called: wiring fails first."""
        raise NotImplementedError


class NotFoundHandler:
    """An exception handler whose exception type is a TYPE_CHECKING-only import."""

    def handle_exception(self, exception: "NotFoundError") -> "ExceptionResponse[Health]":
        """Never called: wiring fails first."""
        raise NotImplementedError


class EndpointApp(BaseApp):
    """Mounts the handler case."""

    async def wire(self) -> None:
        self._include_endpoint(ParamsEndpoint())


class AuthApp(BaseApp):
    """Mounts the authenticator case."""

    async def wire(self) -> None:
        self._include_endpoint(HealthEndpoint(), auth=HeadersAuth())


class WebSocketApp(BaseApp):
    """Mounts the WebSocket case."""

    async def wire(self) -> None:
        self._include_websocket(ParamsWebSocket())


class MiddlewareApp(BaseApp):
    """Mounts the middleware case."""

    async def wire(self) -> None:
        self._include_middleware(RequestObserver())
        self._include_endpoint(HealthEndpoint())


class ExceptionHandlerApp(BaseApp):
    """Mounts the exception-handler case."""

    async def wire(self) -> None:
        self._include_exception_handler(NotFoundHandler())
        self._include_endpoint(HealthEndpoint())


@pytest.mark.parametrize(
    ("app_type", "match"),
    [
        (EndpointApp, "ParamsEndpoint.get: annotation 'WidgetPath' is not defined at runtime"),
        (AuthApp, "HeadersAuth.authenticate: annotation 'Credentials' is not defined"),
        (WebSocketApp, "ParamsWebSocket.handle: annotation 'WidgetPath' is not defined"),
        (MiddlewareApp, "RequestObserver.observe: annotation 'Request' is not defined"),
        (ExceptionHandlerApp, "NotFoundHandler.handle_exception: annotation 'NotFoundError'"),
    ],
    ids=["handler", "authenticate", "websocket", "middleware", "exception-handler"],
)
def test_type_checking_only_annotation_is_a_wiring_error(
    app_type: type[BaseApp], match: str
) -> None:
    """The failure names where the annotation is and says to import it normally."""
    with pytest.raises(RuntimeError, match=match) as raised:
        TestClient(app_type())

    assert "not under `if TYPE_CHECKING:`" in str(raised.value)


async def handle_credentials(item: "Credentials") -> None:
    """A background handler whose item type is a TYPE_CHECKING-only import."""
    raise NotImplementedError


def test_type_checking_only_background_item_is_a_wiring_error() -> None:
    """Background registration reports it the same way."""
    tasks = BackgroundTasks()

    with pytest.raises(WiringError, match="handle_credentials: annotation 'Credentials'"):
        tasks.register(handle_credentials)
