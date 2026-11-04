"""``TestClient`` fails a request whose operation declares ``exceptions=`` but answers with
an error the declaration (and so the OpenAPI spec) leaves out."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, Self, cast

import pytest
from msgspec import Struct

from jero import (
    BaseApp,
    BaseHTTPError,
    Endpoint,
    EndpointMeta,
    HTTPError,
    JSONResponse,
    NDJSONStreamingResponse,
    NoContent,
    OperationMeta,
    Request,
    ResponseSpec,
    StructHTTPError,
)
from jero.testing import TestClient


class Item(Struct):
    """The success body."""

    item_id: str


class ModeParams(Struct):
    """Selects which error the endpoint raises."""

    mode: str


class DeclaredError(HTTPError, type="declared", title="Declared", status=409):
    """The error the checked operation declares."""


class DeclaredSubclassError(
    DeclaredError, type="declared-subclass", title="Declared subclass", status=409
):
    """A subclass of the declared error: it documents its own ``type``, so it isn't covered."""


class UndeclaredError(HTTPError, type="undeclared", title="Undeclared", status=410):
    """An error no operation declares."""


class TakenOverError(HTTPError, type="taken-over", title="Taken over", status=429):
    """An error at a status an explicit ``ResponseSpec`` documents."""


class TeapotBody(Struct):
    """A house-style body for the Struct-family error."""

    message: str


class UndeclaredStructError(
    StructHTTPError[TeapotBody], status=418, description="Teapot", consts={"message": "teapot"}
):
    """A Struct-family error (no ``type`` on the wire) no operation declares."""


class UpstreamFailureError(Exception):
    """A domain exception an exception handler translates into an HTTP error."""


class UpstreamFailureHandler:
    """Translates the domain exception into an error the operation doesn't declare."""

    def handle_exception(self, exception: UpstreamFailureError) -> UndeclaredError:
        """Answer with the undeclared error."""
        _ = exception
        return UndeclaredError()


class CheckedEndpoint(
    Endpoint,
    path="/checked",
    meta=EndpointMeta(exceptions=[DeclaredError]),
    meta_get=OperationMeta(responses=[ResponseSpec(429, "Slow down")]),
):
    """Declares its errors at the class level, so both operations are checked."""

    async def get(self, params: ModeParams) -> Item:
        """Raise the error ``params.mode`` names."""
        errors: dict[str, Exception] = {
            "declared": DeclaredError(),
            "subclass": DeclaredSubclassError(),
            "undeclared": UndeclaredError(),
            "struct": UndeclaredStructError(),
            "taken-over": TakenOverError(),
            "translated": UpstreamFailureError(),
            "crash": RuntimeError("crash"),
        }
        raise errors[params.mode]

    async def post(self, json: Item) -> Item:
        """Echo the body (a malformed one is the framework's own 422)."""
        return json


class UncheckedEndpoint(Endpoint, path="/unchecked"):
    """Declares nothing, so nothing is checked."""

    async def get(self) -> Item:
        """Raise an error no operation declares."""
        raise UndeclaredError()


class StreamEndpoint(Endpoint, path="/stream", meta=EndpointMeta(exceptions=[DeclaredError])):
    """A streaming operation that fails before it streams."""

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Raise an error the operation doesn't declare."""
        raise UndeclaredError()


class FailingStream:
    """A stream that raises an error no operation declares before its first item."""

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Item:
        raise UndeclaredError()


class StreamItemsEndpoint(
    Endpoint, path="/stream-items", meta=EndpointMeta(exceptions=[DeclaredError])
):
    """A streaming operation whose stream fails before its first item."""

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return the failing stream."""
        return NDJSONStreamingResponse(stream=FailingStream())


class UndeclaredErrorsApp(BaseApp):
    """App wiring the checked, unchecked, and streaming operations (no OpenAPI)."""

    async def wire(self) -> None:
        self._include_exception_handler(UpstreamFailureHandler())
        self._include_endpoint(CheckedEndpoint())
        self._include_endpoint(UncheckedEndpoint())
        self._include_endpoint(StreamEndpoint())
        self._include_endpoint(StreamItemsEndpoint())


@pytest.mark.parametrize(
    ("mode", "error", "status"),
    [
        ("undeclared", "UndeclaredError", 410),
        ("subclass", "DeclaredSubclassError", 409),
        ("struct", "UndeclaredStructError", 418),
        ("translated", "UndeclaredError", 410),
    ],
)
def test_undeclared_error_fails_the_request(mode: str, error: str, status: int) -> None:
    """An error outside the declared set fails the request: an undeclared class, a subclass
    of a declared one, a Struct-family error, and one an exception handler translated to."""
    with TestClient(UndeclaredErrorsApp()) as client, pytest.raises(AssertionError) as excinfo:
        client.get("/checked", params={"mode": mode})
    assert str(excinfo.value) == (
        f"GET /checked returned {error} ({status}), which its exceptions=[...] does not declare"
    )


def test_head_request_is_reported_as_head() -> None:
    """The automatic HEAD route runs the GET handler, but its failure names HEAD."""
    with (
        TestClient(UndeclaredErrorsApp()) as client,
        pytest.raises(AssertionError, match=r"^HEAD /checked returned UndeclaredError"),
    ):
        client.head("/checked", params={"mode": "undeclared"})


@pytest.mark.parametrize(
    ("mode", "status"),
    [("declared", 409), ("taken-over", 429), ("crash", 500)],
)
def test_documented_error_passes(mode: str, status: int) -> None:
    """A declared error, one at a status an explicit ``ResponseSpec`` documents, and the
    generic 500 all appear in the spec, so the request answers normally."""
    with TestClient(UndeclaredErrorsApp()) as client:
        assert client.get("/checked", params={"mode": mode}).status_code == status


def test_framework_derived_error_passes() -> None:
    """The framework's own 422 for a malformed body is derived into the spec already."""
    with TestClient(UndeclaredErrorsApp()) as client:
        assert client.post("/checked", json={"wrong": "wrong"}).status_code == 422


def test_operation_declaring_nothing_is_not_checked() -> None:
    """The declaration is the switch: an operation without ``exceptions=`` isn't checked."""
    with TestClient(UndeclaredErrorsApp()) as client:
        assert client.get("/unchecked").status_code == 410


@pytest.mark.parametrize("path", ["/stream", "/stream-items"])
def test_undeclared_error_fails_a_streaming_request(path: str) -> None:
    """A streaming operation is checked the same way, whether the handler raises before
    returning the stream or the stream raises before its first item."""
    with (
        TestClient(UndeclaredErrorsApp()) as client,
        pytest.raises(AssertionError, match=f"GET {path} returned UndeclaredError"),
    ):
        client.stream_get(path)


class CrashAfterResponseApp(UndeclaredErrorsApp):
    """The same app, but it crashes after every HTTP response has been sent."""

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        await super().__call__(scope, receive, send)
        if scope["type"] == "http":
            raise RuntimeError("crash-after-response")


def test_undeclared_error_message_survives_a_later_crash() -> None:
    """A streaming request still fails with the undeclared-error message when the app
    raises after sending the error response; that crash rides along as its context."""
    with (
        TestClient(CrashAfterResponseApp()) as client,
        pytest.raises(AssertionError, match="GET /stream returned UndeclaredError") as excinfo,
    ):
        client.stream_get("/stream")
    assert isinstance(excinfo.value.__context__, RuntimeError)


class BadDeclarationEndpoint(
    Endpoint,
    path="/bad-declaration",
    meta_get=OperationMeta(exceptions=cast(list[type[BaseHTTPError]], [str])),
):
    """Declares a non-error class."""

    async def get(self) -> Item:
        """Return an item."""
        return Item(item_id="item-id")


class BadDeclarationApp(BaseApp):
    """App with an invalid declaration and no OpenAPI wired."""

    async def wire(self) -> None:
        self._include_endpoint(BadDeclarationEndpoint())


def test_invalid_declaration_fails_wiring_without_openapi() -> None:
    """An invalid ``exceptions=`` entry fails at wiring even when OpenAPI isn't wired, since
    the check reads the declaration on every app."""
    with pytest.raises(RuntimeError, match="'exceptions' entries must be concrete jero error"):
        TestClient(BadDeclarationApp())


class _CollectSend:
    """ASGI send that records every message."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@asynccontextmanager
async def _lifespan(app: BaseApp) -> AsyncGenerator[None]:
    """Drive ``app`` through ASGI lifespan startup/shutdown, as a real server does."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await from_app.get()
        await task


@pytest.mark.asyncio
async def test_real_server_request_is_never_checked() -> None:
    """A real server's scope carries no check list, so outside ``TestClient`` an
    undeclared error is answered as usual: the check never affects production."""
    app = UndeclaredErrorsApp()
    send = _CollectSend()
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/checked",
        "query_string": b"mode=undeclared",
        "headers": [],
    }
    async with _lifespan(app):
        await app(scope, asyncio.Queue[dict[str, Any]]().get, send)
    assert send.messages[0]["status"] == 410


class EchoHeaders(Struct):
    """The response header a failing middleware hook would add."""

    x_echo: str


class FailingHeadersMiddleware:
    """A dynamic ``response_headers`` hook that raises an error no operation declares."""

    def response_headers(self, request: Request) -> EchoHeaders | None:
        """Fail while the response's headers are assembled."""
        _ = request
        raise UndeclaredError()


class PlainHeadersEndpoint(Endpoint, path="/plain", meta=EndpointMeta(exceptions=[DeclaredError])):
    """Returns a plain Struct (the inlined fast path)."""

    async def get(self) -> Item:
        """Return an item."""
        return Item(item_id="item-id")


class WrappedHeadersEndpoint(
    Endpoint, path="/wrapped", meta=EndpointMeta(exceptions=[DeclaredError])
):
    """Returns a response wrapper (the sender path)."""

    async def get(self) -> JSONResponse[Item]:
        """Return a wrapped item."""
        return JSONResponse(json=Item(item_id="item-id"))


class EmptyHeadersEndpoint(Endpoint, path="/empty", meta=EndpointMeta(exceptions=[DeclaredError])):
    """Returns a fixed-status wrapper with no body."""

    async def get(self) -> NoContent:
        """Return no content."""
        return NoContent()


class FailingHeadersApp(BaseApp):
    """App scoping the failing hook onto each return shape."""

    async def wire(self) -> None:
        self._include_endpoint(PlainHeadersEndpoint(), middleware=[FailingHeadersMiddleware()])
        self._include_endpoint(WrappedHeadersEndpoint(), middleware=[FailingHeadersMiddleware()])
        self._include_endpoint(EmptyHeadersEndpoint(), middleware=[FailingHeadersMiddleware()])


@pytest.mark.parametrize("path", ["/plain", "/wrapped", "/empty"])
def test_undeclared_error_from_a_header_hook_fails_the_request(path: str) -> None:
    """A middleware header hook raising an undeclared error is checked whatever the
    operation returns: a plain Struct, a response wrapper, or a bodiless wrapper."""
    with (
        TestClient(FailingHeadersApp()) as client,
        pytest.raises(AssertionError, match=f"GET {path} returned UndeclaredError"),
    ):
        client.get(path)
