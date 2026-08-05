"""Typed WebSocket connections and in-process channel fan-out."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Literal, Union, cast, get_args, get_origin

from msgspec import DecodeError, Struct, ValidationError
from msgspec.json import Decoder
from typing_extensions import TypeForm

from jero._wiring_types import WiringError, is_struct_type, unwrap_alias
from jero.codecs import msgspec_encoder

type _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type _Send = Callable[[dict[str, Any]], Awaitable[None]]
type _PayloadKind = Literal["json", "text", "bytes"]
type _Overflow = Literal["close", "drop-oldest"]
type _QueuedFrame = tuple[Literal["frame"], bytes] | tuple[Literal["close"], int]

_STANDARD_CLOSE_CODES = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1011, 1012, 1013, 1014}
)


def _union_members(annotation: object) -> tuple[object, ...]:
    annotation = unwrap_alias(annotation)
    if get_origin(annotation) in (UnionType, Union):
        return tuple(unwrap_alias(member) for member in get_args(annotation))
    return (annotation,)


def _payload_kind(label: str, annotation: object) -> _PayloadKind:
    members = _union_members(annotation)
    if len(members) == 1 and members[0] is str:
        return "text"
    if len(members) == 1 and members[0] is bytes:
        return "bytes"
    if not members or not all(is_struct_type(member) for member in members):
        raise WiringError(
            f"{label} must be str, bytes, or a tagged msgspec.Struct union; got {annotation!r}",
        )
    for member in members:
        struct_type = cast(type[Struct], member)
        tag = struct_type.__struct_config__.tag
        if not isinstance(tag, str) or not tag.strip():
            raise WiringError(
                f"{label}: {struct_type.__name__} must declare an explicit string tag",
            )
    return "json"


class WebSocket[Inbound, Outbound](AsyncIterator[Inbound]):
    """One accepted, typed WebSocket connection."""

    __slots__ = (
        "_closed",
        "_decoder",
        "_inbound_kind",
        "_max_frame_size",
        "_outbound_kind",
        "_receive",
        "_send",
    )

    def __init__(
        self,
        receive: _Receive,
        send: _Send,
        spec: "CompiledWebSocket",
    ) -> None:
        self._receive = receive
        self._send = send
        self._inbound_kind = spec.inbound_kind
        self._outbound_kind = spec.outbound_kind
        self._decoder = spec.decoder
        self._max_frame_size = spec.max_frame_size
        self._closed = False

    def __aiter__(self) -> "WebSocket[Inbound, Outbound]":
        return self

    async def _protocol_close(self, code: int, reason: str) -> None:
        await self.close(code=code, reason=reason)

    async def _frame_payload(self, message: MutableMapping[str, Any]) -> bytes:
        text = message.get("text")
        data = message.get("bytes")
        if self._inbound_kind == "bytes":
            if not isinstance(data, bytes):
                await self._protocol_close(1003, "binary frame required")
                raise StopAsyncIteration
            payload = data
        else:
            if not isinstance(text, str):
                await self._protocol_close(1003, "text frame required")
                raise StopAsyncIteration
            payload = text.encode()
        if len(payload) > self._max_frame_size:
            await self._protocol_close(1009, "message too big")
            raise StopAsyncIteration
        return payload

    async def __anext__(self) -> Inbound:
        if self._closed:
            raise StopAsyncIteration
        message = await self._receive()
        if message["type"] == "websocket.disconnect":
            self._closed = True
            raise StopAsyncIteration
        if message["type"] != "websocket.receive":
            await self._protocol_close(1008, "unexpected WebSocket event")
            raise StopAsyncIteration
        payload = await self._frame_payload(message)
        if self._inbound_kind == "bytes":
            return cast("Inbound", payload)
        if self._inbound_kind == "text":
            return cast("Inbound", payload.decode())
        try:
            decoder = cast("Decoder[object]", self._decoder)
            return cast("Inbound", decoder.decode(payload))
        except ValidationError:
            await self._protocol_close(1008, "invalid payload")
        except DecodeError:
            await self._protocol_close(1007, "malformed JSON")
        raise StopAsyncIteration

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """Close the connection once."""
        if self._closed:
            return
        if code not in _STANDARD_CLOSE_CODES and not 4000 <= code <= 4999:
            raise ValueError(
                "WebSocket close code must be a standard sendable code or in "
                "jero's 4000-4999 application range"
            )
        if len(reason.encode()) > 123:
            raise ValueError("WebSocket close reason must be at most 123 UTF-8 bytes")
        self._closed = True
        sent = False
        try:
            await self._send({"type": "websocket.close", "code": code, "reason": reason})
            sent = True
        finally:
            if not sent:
                self._closed = False

    async def send(self, message: Outbound) -> None:
        """Encode and send one outbound message according to the declared framing."""
        if self._closed:
            raise RuntimeError("cannot send on a closed WebSocket")
        if self._outbound_kind == "bytes":
            await self._send({"type": "websocket.send", "bytes": cast("bytes", message)})
            return
        if self._outbound_kind == "text":
            await self._send({"type": "websocket.send", "text": cast("str", message)})
            return
        await self.send_encoded(msgspec_encoder.encode(message))

    async def send_encoded(self, payload: bytes) -> None:
        """Send pre-encoded JSON; the package boundary used by :class:`Channel`."""
        if self._closed:
            raise RuntimeError("cannot send on a closed WebSocket")
        await self._send({"type": "websocket.send", "text": payload.decode()})


@dataclass(frozen=True, slots=True)
class CompiledWebSocket:
    """The framing contract resolved once at wiring time."""

    inbound_kind: _PayloadKind
    outbound_kind: _PayloadKind
    decoder: Decoder[object] | None
    max_frame_size: int

    def open[Inbound, Outbound](
        self, receive: _Receive, send: _Send
    ) -> WebSocket[Inbound, Outbound]:
        """Create one accepted connection from this compiled framing contract."""
        return WebSocket(receive, send, self)


def compile_websocket(
    inbound: object, outbound: object, *, max_frame_size: int
) -> CompiledWebSocket:
    """Compile and validate both directions of a WebSocket protocol."""
    inbound_kind = _payload_kind("WebSocket inbound type", inbound)
    return CompiledWebSocket(
        inbound_kind=inbound_kind,
        outbound_kind=_payload_kind("WebSocket outbound type", outbound),
        decoder=Decoder(inbound) if inbound_kind == "json" else None,
        max_frame_size=max_frame_size,
    )


@dataclass(eq=False, slots=True)
class _Subscriber[T]:
    websocket: WebSocket[object, T]
    queue: asyncio.Queue[_QueuedFrame]
    topics: set[str] = field(default_factory=set)
    writer: asyncio.Task[None] | None = None


@dataclass(slots=True)
class Subscription[T]:
    """One channel attachment; topics may be joined and left synchronously."""

    _channel: "Channel[T]"
    _subscriber: _Subscriber[T]

    def join(self, topic: str) -> None:
        """Subscribe this attachment to a topic."""
        self._channel.join_subscription(self._subscriber, topic)

    def leave(self, topic: str) -> None:
        """Unsubscribe this attachment from a topic."""
        self._channel.leave_subscription(self._subscriber, topic)


@dataclass(slots=True)
class _Attachment[T](AbstractAsyncContextManager[Subscription[T]]):
    _channel: "Channel[T]"
    _websocket: WebSocket[object, T]
    _subscriber: _Subscriber[T] | None = None

    async def __aenter__(self) -> Subscription[T]:
        self._subscriber = self._channel._attach(self._websocket)
        return Subscription(self._channel, self._subscriber)

    async def __aexit__(self, *exc: object) -> None:
        if self._subscriber is not None:
            await self._channel._detach(self._subscriber)


class Channel[T]:
    """A typed, per-process broadcast channel with bounded per-client queues."""

    def __init__(
        self,
        message_type: TypeForm[T],
        *,
        overflow: _Overflow = "close",
        queue_size: int = 64,
    ) -> None:
        if _payload_kind("Channel message type", message_type) != "json":
            raise WiringError("Channel message type must be a tagged msgspec.Struct union")
        if overflow not in ("close", "drop-oldest"):
            raise WiringError("Channel overflow must be 'close' or 'drop-oldest'")
        if not isinstance(queue_size, int) or isinstance(queue_size, bool) or queue_size < 1:
            raise WiringError("Channel queue_size must be a positive integer")
        self._overflow = overflow
        self._queue_size = queue_size
        self._topics: dict[str, set[_Subscriber[T]]] = {}

    async def _write(self, subscriber: _Subscriber[T]) -> None:
        try:
            while True:
                item = await subscriber.queue.get()
                if item[0] == "close":
                    await subscriber.websocket.close(code=item[1], reason="channel overflow")
                    return
                await subscriber.websocket.send_encoded(item[1])
        except OSError:
            return

    def _attach(self, websocket: WebSocket[object, T]) -> _Subscriber[T]:
        subscriber = _Subscriber(websocket, asyncio.Queue(self._queue_size))
        subscriber.writer = asyncio.create_task(self._write(subscriber))
        return subscriber

    async def _detach(self, subscriber: _Subscriber[T]) -> None:
        for topic in tuple(subscriber.topics):
            self.leave_subscription(subscriber, topic)
        if subscriber.writer is not None:
            subscriber.writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscriber.writer

    def join_subscription(self, subscriber: _Subscriber[T], topic: str) -> None:
        """Join helper crossing the public Subscription-to-Channel boundary."""
        if topic in subscriber.topics:
            return
        subscriber.topics.add(topic)
        self._topics.setdefault(topic, set()).add(subscriber)

    def leave_subscription(self, subscriber: _Subscriber[T], topic: str) -> None:
        """Leave helper crossing the public Subscription-to-Channel boundary."""
        if topic not in subscriber.topics:
            return
        subscriber.topics.remove(topic)
        subscribers = self._topics[topic]
        subscribers.remove(subscriber)
        if not subscribers:
            del self._topics[topic]

    def attach[Inbound](self, websocket: WebSocket[Inbound, T]) -> _Attachment[T]:
        """Attach a connection for the lifetime of an async context manager."""
        return _Attachment(self, cast("WebSocket[object, T]", websocket))

    def publish(self, topic: str, message: T) -> None:
        """Encode once and enqueue for every subscriber without awaiting clients."""
        subscribers = self._topics.get(topic)
        if not subscribers:
            return
        frame: _QueuedFrame = ("frame", msgspec_encoder.encode(message))
        for subscriber in tuple(subscribers):
            try:
                subscriber.queue.put_nowait(frame)
            except asyncio.QueueFull:
                subscriber.queue.get_nowait()
                if self._overflow == "drop-oldest":
                    subscriber.queue.put_nowait(frame)
                else:
                    subscriber.queue.put_nowait(("close", 1013))
