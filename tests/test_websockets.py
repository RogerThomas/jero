"""Typed WebSockets through the public TestClient ASGI boundary."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import pytest
from msgspec import Struct

from demo_app.models import PingRequest, PingResponse
from jero import (
    BaseApp,
    Channel,
    ErrorBodyAdapter,
    HTTPError,
    HTTPMethod,
    JSONResponse,
    RawHeaders,
    Request,
    WebSocket,
    WebSocketEndpoint,
    WiringError,
)
from jero.testing import (
    TestClient,
    TestWebSocket,
    WebSocketClosedError,
    WebSocketUpgradeError,
)


def test_typed_websocket_round_trip(client: TestClient) -> None:
    """A tagged request round-trips with bound path and authenticated user data."""
    websocket: TestWebSocket[PingRequest, PingResponse]
    with client.websocket(
        "/websocket/client-id",
        inbound=PingRequest,
        outbound=PingResponse,
        headers={"authorization": "Bearer token"},
    ) as websocket:
        websocket.send(PingRequest(request_id="request-id", message="message"))
        assert websocket.receive() == PingResponse(
            request_id="request-id",
            client_id="client-id",
            user_id="user-id",
            message="message",
        )


def test_websocket_auth_rejects_before_upgrade(client: TestClient) -> None:
    """Authentication failure remains an HTTP response before the upgrade."""
    with pytest.raises(WebSocketUpgradeError) as caught:
        client.websocket("/websocket/client-id", inbound=PingRequest, outbound=PingResponse)
    assert caught.value.response.status_code == 401
    assert caught.value.response.headers["content-type"] == "application/json"


def test_websocket_rejection_without_denial_extension(client: TestClient) -> None:
    """A server without the optional denial extension receives a pre-accept close."""
    with pytest.raises(WebSocketClosedError) as caught:
        client.websocket(
            "/websocket/client-id",
            inbound=PingRequest,
            outbound=PingResponse,
            denial_response_extension=False,
        )
    assert caught.value.code == 1008


def _send_malformed_json(websocket: TestWebSocket[PingRequest, PingResponse]) -> None:
    websocket.send_text("{")


def _send_invalid_payload(websocket: TestWebSocket[PingRequest, PingResponse]) -> None:
    websocket.send_text('{"type":"ping-request"}')


def _send_binary_frame(websocket: TestWebSocket[PingRequest, PingResponse]) -> None:
    websocket.send_bytes(b"message")


@pytest.mark.parametrize(
    ("send", "code"),
    [
        (_send_malformed_json, 1007),
        (_send_invalid_payload, 1008),
        (_send_binary_frame, 1003),
    ],
)
def test_typed_websocket_protocol_closes(
    client: TestClient,
    send: Callable[[TestWebSocket[PingRequest, PingResponse]], None],
    code: int,
) -> None:
    """Protocol violations close with their precise standard close codes."""
    websocket: TestWebSocket[PingRequest, PingResponse]
    with client.websocket(
        "/websocket/client-id",
        inbound=PingRequest,
        outbound=PingResponse,
        headers={"authorization": "Bearer token"},
    ) as websocket:
        send(websocket)
        with pytest.raises(WebSocketClosedError) as caught:
            websocket.receive()
        assert caught.value.code == code


class TinyMessage(Struct, tag="tiny-message"):
    """Small inbound model for framing and size tests."""

    value: str


class TinyReply(Struct, tag="tiny-reply"):
    """Small outbound model for framing and size tests."""

    value: str


class TinyWebSocket(WebSocketEndpoint, path="/tiny"):
    """Echo TinyMessage values as TinyReply values."""

    async def handle(self, websocket: WebSocket[TinyMessage, TinyReply]) -> None:
        """Echo every valid inbound value."""
        async for message in websocket:
            await websocket.send(TinyReply(value=message.value))


class TinyApp(BaseApp):
    """Mount TinyWebSocket with a deliberately small frame limit."""

    async def wire(self) -> None:
        """Wire the size-limited test protocol."""
        self._include_websocket(TinyWebSocket(), max_frame_size=8)


class BinaryToTextWebSocket(WebSocketEndpoint, path="/binary-text"):
    """Convert raw binary inbound frames to raw text outbound frames."""

    async def handle(self, websocket: WebSocket[bytes, str]) -> None:
        """Decode each binary message as text."""
        async for message in websocket:
            await websocket.send(message.decode())


class TextToBinaryWebSocket(WebSocketEndpoint, path="/text-binary"):
    """Convert raw text inbound frames to raw binary outbound frames."""

    async def handle(self, websocket: WebSocket[str, bytes]) -> None:
        """Encode each text message as bytes."""
        async for message in websocket:
            await websocket.send(message.encode())


class ClosingWebSocket(WebSocketEndpoint, path="/closing"):
    """Close twice to prove repeated close calls are harmless."""

    async def handle(self, websocket: WebSocket[str, str]) -> None:
        """Send one close event even when called twice."""
        await websocket.close(code=4000, reason="closed")
        await websocket.close(code=4000, reason="closed")


class SendAfterCloseWebSocket(WebSocketEndpoint, path="/send-after-close"):
    """Exercise the guard against a data frame after the close event."""

    async def handle(self, websocket: WebSocket[str, str]) -> None:
        """Close, then verify that a later send is rejected locally."""
        await websocket.close(code=4000)
        try:
            await websocket.send("late")
        except RuntimeError:
            return
        raise AssertionError("send after close unexpectedly succeeded")


class RawFramingApp(BaseApp):
    """Mount both cross-kind raw framing protocols."""

    async def wire(self) -> None:
        """Wire the binary/text framing combinations."""
        self._include_websocket(BinaryToTextWebSocket())
        self._include_websocket(TextToBinaryWebSocket())
        self._include_websocket(ClosingWebSocket())
        self._include_websocket(SendAfterCloseWebSocket())


class InvalidCodeWebSocket(WebSocketEndpoint, path="/invalid-code"):
    """Attempt an invalid close code so the framework must fall back to 1011."""

    async def handle(self, websocket: WebSocket[str, str]) -> None:
        """Try to close with a reserved, unsendable code."""
        await websocket.close(code=1005)


class InvalidReasonWebSocket(WebSocketEndpoint, path="/invalid-reason"):
    """Attempt an overlong close reason so the framework must fall back to 1011."""

    async def handle(self, websocket: WebSocket[str, str]) -> None:
        """Try to close with more than 123 UTF-8 bytes."""
        await websocket.close(reason="é" * 62)


class ClientOnlyCodeWebSocket(WebSocketEndpoint, path="/client-only-code"):
    """Attempt the client-only extension-negotiation close code 1010."""

    async def handle(self, websocket: WebSocket[str, str]) -> None:
        """Try to emit code 1010 from the server side."""
        await websocket.close(code=1010)


class InvalidCloseApp(BaseApp):
    """Mount both invalid-close handlers."""

    async def wire(self) -> None:
        """Wire invalid close attempts for strict validation tests."""
        self._include_websocket(InvalidCodeWebSocket())
        self._include_websocket(InvalidReasonWebSocket())
        self._include_websocket(ClientOnlyCodeWebSocket())


class Denied(Struct):
    """A typed middleware handshake-denial body."""

    reason: str


class HouseError(Struct):
    """A non-Problem app-wide error body."""

    code: str


class HouseErrorAdapter(ErrorBodyAdapter[HouseError]):
    """Render Problem-family failures in the house shape."""

    def compose(self, error: HTTPError) -> HouseError:
        """Translate the error's machine type into the house body."""
        return HouseError(code=error.type)


class AdapterApp(BaseApp):
    """Install a house error adapter for unmatched WebSocket handshakes."""

    async def wire(self) -> None:
        """Wire only the error adapter, leaving every WebSocket path unmatched."""
        self._include_error_adapter(HouseErrorAdapter())


class DenyHandshake:
    """Reject every GET handshake with a typed 403 response."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    def intercept(self, request: Request) -> JSONResponse[Denied]:
        """Reject the handshake and echo its path in the body."""
        return JSONResponse(json=Denied(reason=request.path), status_code=403)


class GlobalMiddlewareApp(BaseApp):
    """Apply the denying middleware globally."""

    async def wire(self) -> None:
        """Wire a globally intercepted WebSocket."""
        self._include_middleware(DenyHandshake())
        self._include_websocket(TinyWebSocket())


class ScopedMiddlewareApp(BaseApp):
    """Apply the denying middleware to one WebSocket mount."""

    async def wire(self) -> None:
        """Wire an include-scoped intercepted WebSocket."""
        self._include_websocket(TinyWebSocket(), middleware=(DenyHandshake(),))


class HeaderOnlyMiddleware:
    """A scoped middleware without an intercept capability."""

    response_headers = Denied(reason="constant")


class PostOnlyHandshake:
    """Declare an intercept that cannot run on a GET handshake."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("POST",)

    def intercept(self, request: Request) -> JSONResponse[Denied]:
        """Return a response if the invalid method scope were reachable."""
        return JSONResponse(json=Denied(reason=request.path), status_code=403)


class HeaderMiddlewareApp(BaseApp):
    """Mount a socket with non-intercepting scoped middleware."""

    async def wire(self) -> None:
        """Wire the middleware whose irrelevant capability is skipped."""
        self._include_websocket(TinyWebSocket(), middleware=(HeaderOnlyMiddleware(),))


class PostOnlyMiddlewareApp(BaseApp):
    """Attempt to mount a socket with a POST-only intercept."""

    async def wire(self) -> None:
        """Wire the invalid handshake intercept scope."""
        self._include_websocket(TinyWebSocket(), middleware=(PostOnlyHandshake(),))


def test_websocket_max_frame_size() -> None:
    """An oversized inbound frame closes with message-too-big code 1009."""
    with TestClient(TinyApp()) as client:
        websocket: TestWebSocket[TinyMessage, TinyReply]
        with client.websocket("/tiny", inbound=TinyMessage, outbound=TinyReply) as websocket:
            websocket.send_text("123456789")
            with pytest.raises(WebSocketClosedError) as caught:
                websocket.receive()
            assert caught.value.code == 1009


def test_websocket_frame_kinds_are_independent() -> None:
    """Inbound and outbound text/binary framing are independently compiled."""
    with TestClient(RawFramingApp()) as client:
        binary_text: TestWebSocket[bytes, str]
        with client.websocket("/binary-text", inbound=bytes, outbound=str) as binary_text:
            binary_text.send(b"message")
            assert binary_text.receive() == "message"

        text_binary: TestWebSocket[str, bytes]
        with client.websocket("/text-binary", inbound=str, outbound=bytes) as text_binary:
            text_binary.send("message")
            assert text_binary.receive() == b"message"

        with client.websocket("/binary-text", inbound=bytes, outbound=str) as binary_text:
            binary_text.send_text("wrong-kind")
            with pytest.raises(WebSocketClosedError) as caught:
                binary_text.receive()
            assert caught.value.code == 1003


def test_repeated_websocket_close_is_idempotent() -> None:
    """Only the first close call emits an event and fixes the application code."""
    with TestClient(RawFramingApp()) as client:
        websocket: TestWebSocket[str, str]
        with client.websocket("/closing", inbound=str, outbound=str) as websocket:
            with pytest.raises(WebSocketClosedError) as caught:
                websocket.receive()
            assert caught.value.code == 4000
            with pytest.raises(RuntimeError, match="closed test WebSocket"):
                websocket.send("late")
            with pytest.raises(RuntimeError, match="closed test WebSocket"):
                websocket.send_text("late")
            with pytest.raises(RuntimeError, match="closed test WebSocket"):
                websocket.send_bytes(b"late")


def test_websocket_rejects_send_after_close() -> None:
    """A handler cannot emit a data frame after its close event."""
    with TestClient(RawFramingApp()) as client:
        websocket: TestWebSocket[str, str]
        with client.websocket("/send-after-close", inbound=str, outbound=str) as websocket:
            with pytest.raises(WebSocketClosedError) as caught:
                websocket.receive()
            assert caught.value.code == 4000


@pytest.mark.parametrize("path", ["/invalid-code", "/invalid-reason", "/client-only-code"])
def test_invalid_close_becomes_internal_error(path: str) -> None:
    """Invalid public close arguments remain sendable as the handler's 1011 failure."""
    with TestClient(InvalidCloseApp()) as client:
        websocket: TestWebSocket[str, str]
        with client.websocket(path, inbound=str, outbound=str) as websocket:
            with pytest.raises(WebSocketClosedError) as caught:
                websocket.receive()
            assert caught.value.code == 1011


def test_websocket_rejection_uses_adapter_body_and_json_media_type() -> None:
    """A handshake rejection preserves the configured house error representation."""
    with TestClient(AdapterApp()) as client:
        with pytest.raises(WebSocketUpgradeError) as caught:
            client.websocket("/missing", inbound=str, outbound=str)
        assert caught.value.response.headers["content-type"] == "application/json"
        assert caught.value.response.json() == {"code": "not-found"}


@pytest.mark.parametrize("app", [GlobalMiddlewareApp(), ScopedMiddlewareApp()])
def test_middleware_intercepts_websocket_handshake(app: BaseApp) -> None:
    """Global and include-scoped intercepts can deny a handshake before auth."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketUpgradeError) as caught:
            client.websocket("/tiny", inbound=TinyMessage, outbound=TinyReply)
        assert caught.value.response.status_code == 403
        assert caught.value.response.json() == {"reason": "/tiny"}


def test_middleware_denial_without_extension_closes_once() -> None:
    """Middleware denial falls back to one close when denial responses are unsupported."""
    with TestClient(GlobalMiddlewareApp()) as client:
        with pytest.raises(WebSocketClosedError) as caught:
            client.websocket(
                "/tiny",
                inbound=TinyMessage,
                outbound=TinyReply,
                denial_response_extension=False,
            )
        assert caught.value.code == 1008


def test_websocket_skips_middleware_without_intercept() -> None:
    """A scoped middleware with no intercept does not alter the socket protocol."""
    with TestClient(HeaderMiddlewareApp()) as client:
        websocket: TestWebSocket[TinyMessage, TinyReply]
        with client.websocket("/tiny", inbound=TinyMessage, outbound=TinyReply) as websocket:
            websocket.send(TinyMessage(value="ok"))
            assert websocket.receive() == TinyReply(value="ok")


def test_websocket_rejects_intercept_without_get_scope() -> None:
    """A scoped intercept must include GET to affect a WebSocket handshake."""
    with pytest.raises(RuntimeError, match="expected GET"):
        TestClient(PostOnlyMiddlewareApp())


def test_dynamic_websocket_static_mismatch_is_not_found(client: TestClient) -> None:
    """A same-length dynamic route with different static segments does not match."""
    with pytest.raises(WebSocketUpgradeError) as caught:
        client.websocket("/not-websocket/client-id", inbound=PingRequest, outbound=PingResponse)
    assert caught.value.response.status_code == 404


class Untagged(Struct):
    """An intentionally invalid untagged socket message."""

    value: str


class UntaggedWebSocket(WebSocketEndpoint, path="/untagged"):
    """Declare the invalid untagged model as an inbound protocol."""

    async def handle(self, websocket: WebSocket[Untagged, TinyReply]) -> None:
        """Consume messages if wiring were incorrectly allowed to succeed."""
        async for _ in websocket:
            pass


class UntaggedApp(BaseApp):
    """Attempt to mount the invalid untagged protocol."""

    async def wire(self) -> None:
        """Wire the protocol that startup must reject."""
        self._include_websocket(UntaggedWebSocket())


def test_websocket_requires_explicit_struct_tags() -> None:
    """Every Struct message requires an explicit stable string tag."""
    with pytest.raises(RuntimeError, match="must declare an explicit string tag"):
        TestClient(UntaggedApp())


class Publish(Struct, tag="publish"):
    """Ask the channel test endpoint to publish a burst."""

    count: int


class Event(Struct, tag="event"):
    """One channel broadcast carrying its burst index."""

    index: int


class FailingWebSocketTransport:
    """ASGI transport that disappears after accepting one inbound frame."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._received = 0
        self._failed = asyncio.Event()

    async def receive(self) -> dict[str, Any]:
        """Produce connect, one data frame, then disconnect after the failed send."""
        self._received += 1
        if self._received == 1:
            return {"type": "websocket.connect"}
        if self._received == 2:
            return {"type": "websocket.receive", "text": self._text}
        await self._failed.wait()
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(self, message: dict[str, Any]) -> None:
        """Accept the upgrade, then fail every attempted transport write."""
        if message["type"] == "websocket.accept":
            return
        self._failed.set()
        raise OSError("transport closed")


@dataclass
class RoomWebSocket(WebSocketEndpoint, path="/room"):
    """Attach each connection to the shared global room."""

    _room: Channel[Event]

    async def handle(self, websocket: WebSocket[Publish, Event]) -> None:
        """Publish the requested burst to every attached connection."""
        async with self._room.attach(websocket) as subscription:
            subscription.leave("missing")
            subscription.join("global")
            subscription.join("global")
            async for message in websocket:
                for index in range(message.count):
                    self._room.publish("global", Event(index=index))


class RoomApp(BaseApp):
    """Use drop-oldest overflow for the test room."""

    async def wire(self) -> None:
        """Wire a one-frame drop-oldest channel."""
        room = Channel(Event, overflow="drop-oldest", queue_size=1)
        self._include_websocket(RoomWebSocket(room))


class ClosingRoomApp(BaseApp):
    """Use close-on-overflow for the test room."""

    async def wire(self) -> None:
        """Wire a one-frame close-on-overflow channel."""
        room = Channel(Event, overflow="close", queue_size=1)
        self._include_websocket(RoomWebSocket(room))


def _failing_scope(path: str) -> dict[str, Any]:
    """Build one minimal ASGI WebSocket scope for transport-failure tests."""
    return {
        "type": "websocket",
        "path": path,
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
        "extensions": {},
    }


@pytest.mark.asyncio
async def test_handler_transport_failure_is_contained() -> None:
    """A vanished transport cannot escape through handler recovery close."""
    app = TinyApp()
    transport = FailingWebSocketTransport('{"type":"tiny-message","value":"ok"}')
    with TestClient(app):
        await app(_failing_scope("/tiny"), transport.receive, transport.send)


@pytest.mark.asyncio
async def test_channel_writer_transport_failure_is_contained() -> None:
    """A channel writer exits quietly when its subscriber transport vanishes."""
    app = RoomApp()
    transport = FailingWebSocketTransport('{"type":"publish","count":1}')
    with TestClient(app):
        await app(_failing_scope("/room"), transport.receive, transport.send)


def test_channel_fans_out_and_drops_oldest() -> None:
    """A burst reaches all subscribers once, retaining only its newest event."""
    with TestClient(RoomApp()) as client:
        first: TestWebSocket[Publish, Event]
        second: TestWebSocket[Publish, Event]
        with (
            client.websocket("/room", inbound=Publish, outbound=Event) as first,
            client.websocket("/room", inbound=Publish, outbound=Event) as second,
        ):
            first.send(Publish(count=3))
            assert first.receive() == Event(index=2)
            assert second.receive() == Event(index=2)


def test_channel_closes_overflowing_consumer() -> None:
    """The close overflow policy disconnects a slow consumer with code 1013."""
    with TestClient(ClosingRoomApp()) as client:
        websocket: TestWebSocket[Publish, Event]
        with client.websocket("/room", inbound=Publish, outbound=Event) as websocket:
            websocket.send(Publish(count=2))
            with pytest.raises(WebSocketClosedError) as caught:
                websocket.receive()
            assert caught.value.code == 1013


@pytest.mark.parametrize("queue_size", [0, True, 1.5, "1"])
def test_channel_rejects_invalid_queue_size(queue_size: object) -> None:
    """Channel queue size must be a positive, non-boolean integer."""
    channel_factory = cast("Callable[..., Channel[Event]]", Channel)
    with pytest.raises(WiringError, match="queue_size"):
        channel_factory(Event, queue_size=queue_size)


def test_channel_publish_without_subscribers_is_a_noop() -> None:
    """Publishing to an empty topic returns without encoding or queue work."""
    channel = Channel(Event)
    channel.publish("missing", Event(index=1))


class MixedWebSocket(WebSocketEndpoint, path="/mixed"):
    """Declare a protocol whose inbound union mixes framing kinds."""

    async def handle(self, websocket: WebSocket[TinyMessage | str, TinyReply]) -> None:
        """Consume messages if the invalid protocol were wired."""
        async for _ in websocket:
            pass


class NoWebSocketArgument(WebSocketEndpoint, path="/bad-first"):
    """Declare a handler without the required first WebSocket argument."""

    async def handle(self, params: TinyMessage) -> None:
        """Exist only to exercise startup contract validation."""


class BareWebSocketArgument(WebSocketEndpoint, path="/bare"):
    """Declare a handler with an imprecise WebSocket annotation."""

    async def handle(self, websocket: object) -> None:
        """Exist only to exercise startup contract validation."""


class ReturningWebSocket(WebSocketEndpoint, path="/returning"):
    """Declare a WebSocket handler with a value return contract."""

    async def handle(self, websocket: WebSocket[str, str]) -> str:
        """Return a value if the invalid protocol were invoked."""
        del websocket
        return "invalid"


class SyncWebSocket(WebSocketEndpoint, path="/sync"):
    """Declare a synchronous WebSocket handler."""

    def handle(self, websocket: WebSocket[str, str]) -> None:
        """Exist only to exercise startup contract validation."""
        del websocket


class BodyWebSocket(WebSocketEndpoint, path="/body"):
    """Declare an unsupported post-upgrade body source."""

    async def handle(self, websocket: WebSocket[str, str], json: TinyMessage) -> None:
        """Exist only to exercise startup contract validation."""


class BadRawHeadersWebSocket(WebSocketEndpoint, path="/bad-raw-headers"):
    """Declare raw headers with the wrong annotation."""

    async def handle(self, websocket: WebSocket[str, str], raw_headers: bytes) -> None:
        """Exist only to exercise startup contract validation."""


class MissingHandle:
    """Endpoint-shaped object with a path but no protocol handler."""

    path = "/missing-handle"


class InvalidEndpointApp(BaseApp):
    """Mount one endpoint supplied by a wiring-contract test."""

    def __init__(self, endpoint: WebSocketEndpoint) -> None:
        super().__init__()
        self._endpoint = endpoint

    async def wire(self) -> None:
        """Attempt to mount the supplied endpoint."""
        self._include_websocket(self._endpoint)


class InvalidFrameLimitApp(BaseApp):
    """Attempt to wire a socket with a supplied frame limit."""

    def __init__(self, frame_limit: object) -> None:
        super().__init__()
        self._frame_limit = frame_limit

    async def wire(self) -> None:
        """Pass the deliberately invalid limit through the public mount API."""
        include = cast("Callable[..., None]", self._include_websocket)
        include(TinyWebSocket(), max_frame_size=self._frame_limit)


class DuplicateStaticApp(BaseApp):
    """Attempt to register one static WebSocket path twice."""

    async def wire(self) -> None:
        """Wire two endpoints with the same static route shape."""
        self._include_websocket(TinyWebSocket())
        self._include_websocket(TinyWebSocket())


class OtherPath(Struct):
    """Path source for the deliberately colliding dynamic route."""

    other: str


class OtherDynamicWebSocket(WebSocketEndpoint, path="/websocket/{other}"):
    """Declare a route colliding with the demo socket's dynamic shape."""

    async def handle(self, websocket: WebSocket[str, str], path: OtherPath) -> None:
        """Exist only to exercise duplicate route validation."""


class DuplicateDynamicApp(BaseApp):
    """Attempt to register two equivalent dynamic WebSocket routes."""

    async def wire(self) -> None:
        """Wire both colliding dynamic route shapes."""
        self._include_websocket(OtherDynamicWebSocket())
        self._include_websocket(OtherDynamicWebSocket())


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (MixedWebSocket(), "tagged msgspec.Struct union"),
        (NoWebSocketArgument(), "first argument"),
        (BareWebSocketArgument(), "must be annotated"),
        (ReturningWebSocket(), "must return None"),
        (SyncWebSocket(), "must be async"),
        (BodyWebSocket(), "unsupported argument"),
        (BadRawHeadersWebSocket(), "must be annotated as RawHeaders"),
        (cast("WebSocketEndpoint", object()), "no path"),
        (cast("WebSocketEndpoint", MissingHandle()), "must define handle"),
    ],
)
def test_websocket_rejects_invalid_handler_contracts(
    endpoint: WebSocketEndpoint, message: str
) -> None:
    """Invalid framing and handshake declarations fail during startup."""
    with pytest.raises(RuntimeError, match=message):
        TestClient(InvalidEndpointApp(endpoint))


@pytest.mark.parametrize("frame_limit", [0, True, 1.5])
def test_websocket_rejects_invalid_frame_limits(frame_limit: object) -> None:
    """A frame limit must be a positive, non-boolean integer."""
    with pytest.raises(RuntimeError, match="positive integer"):
        TestClient(InvalidFrameLimitApp(frame_limit))


@pytest.mark.parametrize("app", [DuplicateStaticApp(), DuplicateDynamicApp()])
def test_websocket_rejects_duplicate_routes(app: BaseApp) -> None:
    """Static and dynamic WebSocket route shapes may be registered only once."""
    with pytest.raises(RuntimeError, match="already registered"):
        TestClient(app)


class RawHeadersWebSocket(WebSocketEndpoint, path="/raw-headers"):
    """Read the opaque handshake headers source."""

    async def handle(self, websocket: WebSocket[str, str], raw_headers: RawHeaders) -> None:
        """Return the first raw header value."""
        await websocket.send(cast("str", raw_headers.get("x-test")))


class RawHeadersApp(BaseApp):
    """Mount the raw-header handshake binding example."""

    async def wire(self) -> None:
        """Wire the raw-header endpoint."""
        self._include_websocket(RawHeadersWebSocket())


def test_websocket_binds_raw_headers() -> None:
    """The opaque raw handshake headers bag reaches the handler."""
    with TestClient(RawHeadersApp()) as client:
        websocket: TestWebSocket[str, str]
        with client.websocket(
            "/raw-headers", inbound=str, outbound=str, headers={"x-test": "value"}
        ) as websocket:
            assert websocket.receive() == "value"


@pytest.mark.parametrize("message_type", [str, bytes])
def test_channel_rejects_raw_message_types(message_type: object) -> None:
    """Channels accept only tagged JSON Struct protocols."""
    channel_factory = cast("Callable[..., Channel[Event]]", Channel)
    with pytest.raises(WiringError, match=r"tagged msgspec\.Struct union"):
        channel_factory(message_type)


def test_channel_rejects_unknown_overflow_policy() -> None:
    """Channel overflow policy is a closed enumeration."""
    channel_factory = cast("Callable[..., Channel[Event]]", Channel)
    with pytest.raises(WiringError, match="overflow"):
        channel_factory(Event, overflow="discard")
