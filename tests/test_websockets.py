"""Typed WebSockets through the public TestClient ASGI boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pytest
from msgspec import Struct

from demo_app.models import PingRequest, PingResponse
from jero import (
    BaseApp,
    Channel,
    HTTPMethod,
    JSONResponse,
    Request,
    WebSocket,
    WebSocketEndpoint,
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


class RawFramingApp(BaseApp):
    """Mount both cross-kind raw framing protocols."""

    async def wire(self) -> None:
        """Wire the binary/text framing combinations."""
        self._include_websocket(BinaryToTextWebSocket())
        self._include_websocket(TextToBinaryWebSocket())


class Denied(Struct):
    """A typed middleware handshake-denial body."""

    reason: str


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


@pytest.mark.parametrize("app", [GlobalMiddlewareApp(), ScopedMiddlewareApp()])
def test_middleware_intercepts_websocket_handshake(app: BaseApp) -> None:
    """Global and include-scoped intercepts can deny a handshake before auth."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketUpgradeError) as caught:
            client.websocket("/tiny", inbound=TinyMessage, outbound=TinyReply)
        assert caught.value.response.status_code == 403
        assert caught.value.response.json() == {"reason": "/tiny"}


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


@dataclass
class RoomWebSocket(WebSocketEndpoint, path="/room"):
    """Attach each connection to the shared global room."""

    _room: Channel[Event]

    async def handle(self, websocket: WebSocket[Publish, Event]) -> None:
        """Publish the requested burst to every attached connection."""
        async with self._room.attach(websocket) as subscription:
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
