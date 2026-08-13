# WebSockets

Use a WebSocket when the protocol is genuinely bidirectional: the client and server
both initiate messages over one long-lived connection. For one-way server push, prefer
[Server-Sent Events](streaming.md#server-sent-events). SSE reconnects naturally, works
through ordinary HTTP infrastructure, and is usually the simpler contract.

jero WebSockets are typed in both directions. Their framing, decoders, path binding,
headers, and authentication are compiled at wiring time; a handler only receives an
accepted connection after its handshake has passed those checks.

## A complete typed protocol

```python
from typing import assert_never

from msgspec import Struct

from jero import BaseApp, WebSocket, WebSocketEndpoint


class WeatherRequest(Struct, tag="weather-request"):
    request_id: str
    city: str


class ForecastRequest(Struct, tag="forecast-request"):
    request_id: str
    city: str


type Inbound = WeatherRequest | ForecastRequest


class WeatherResponse(Struct, tag="weather-response"):
    request_id: str
    temperature_c: float


class ForecastResponse(Struct, tag="forecast-response"):
    request_id: str
    summary: str


type Outbound = WeatherResponse | ForecastResponse


class WeatherWebSocket(WebSocketEndpoint, path="/weather"):
    async def handle(self, websocket: WebSocket[Inbound, Outbound]) -> None:
        async for message in websocket:
            match message:
                case WeatherRequest(request_id=request_id):
                    # In a class pattern, ``request_id=name`` extracts the attribute;
                    # it is not an assignment to the object.
                    await websocket.send(
                        WeatherResponse(request_id=request_id, temperature_c=20.0)
                    )
                case ForecastRequest(request_id=request_id):
                    await websocket.send(
                        ForecastResponse(request_id=request_id, summary="summary")
                    )
                case _:
                    assert_never(message)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_websocket(WeatherWebSocket())


app = App()
```

Explicit kebab-case tags are required even when a direction currently has one Struct.
The tag is a deployed wire contract: adding a second member later must not silently
change the first message's representation. A final `assert_never` arm makes adding a
new inbound member without handling it a static error.

Correlated ask/answer messages conventionally use `XRequest` / `XResponse` and carry a
`request_id: str` that the server echoes. Unsolicited server-push messages use plain
names such as `Tick` or `Departed`.

## Handshake binding and authentication

`handle` may bind `path`, `params`, `headers`, `cookies`, `raw_headers`, and `user`
exactly like an HTTP handler. `cookies` is the motivating case for cookie auth here: a
browser's WebSocket API cannot set an `Authorization` header, but it always sends
cookies — mount a `CookieAuth`/`HybridAuth` authenticator (see
[Cookies](cookies.md#websockets-the-motivating-case)) and a browser client
authenticates with the session cookie it already has. Authentication and binding run
before the upgrade. A failure is returned as an ordinary typed HTTP rejection when the
ASGI server advertises the WebSocket denial response extension. Without that optional extension, jero sends a pre-accept close and
the server rejects the upgrade (normally as HTTP 403, without the typed body). Only a
valid handshake receives `websocket.accept` and enters the handler.

Acceptance is therefore implicit—there is no `websocket.accept()`. Reject application
state that a browser must understand after acceptance by sending a typed message and
then closing:

```python
await websocket.send(RoomFull(retry_after_seconds=30))
await websocket.close(code=4003, reason="room full")
return
```

Codes `4000–4999` are available to the application. jero uses `1003` for the wrong frame
kind, `1007` for malformed JSON, `1008` for a schema violation, `1009` for an oversized
message, `1011` for an uncaught handler error, and `1013` for a channel overflow close.
Inbound frames default to at most 1 MiB; override this per mount with
`max_frame_size=`. Ping/pong keepalive belongs to the ASGI server because standard ASGI
does not expose ping frames to applications.

`close()` accepts jero's standard protocol codes or application codes `4000–4999`.
Close reasons are limited to the WebSocket protocol's 123 UTF-8 bytes; invalid values
raise before a close event is sent. Code `1010` is client-only and therefore rejected
for these server-originated closes. Sending after the connection starts closing is also
rejected locally rather than emitting an invalid ASGI event.

## Framing

Each direction chooses exactly one kind:

| Declared type | Text frame | Binary frame |
| --- | --- | --- |
| tagged Struct or tagged Struct union | strict JSON | close `1003` |
| `str` | text verbatim | close `1003` |
| `bytes` | close `1003` | bytes verbatim |

The directions are independent, so `WebSocket[bytes, Transcript]` is valid. Mixed forms
such as `Message | str` are not. Raw `str` and `bytes` are deliberate escape hatches for
third-party JSON, msgpack, audio, or another user-owned protocol.

WebSockets are not represented by OpenAPI. The tagged message unions are the
client-facing contract; AsyncAPI generation is outside jero's current scope.

## Broadcasting with `Channel`

`Channel[T]` encodes a message once and fans the same JSON frame out to every local
subscriber. Each connection gets a bounded writer queue, so one slow client never
blocks publishing to the others:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Channel, WebSocket, WebSocketEndpoint


class ChatMessage(Struct, tag="chat-message"):
    text: str


class SendMessage(Struct, tag="send-message"):
    text: str


@dataclass
class ChatWebSocket(WebSocketEndpoint, path="/chat"):
    _room: Channel[ChatMessage]

    async def handle(self, websocket: WebSocket[SendMessage, ChatMessage]) -> None:
        async with self._room.attach(websocket) as subscription:
            subscription.join("global")
            async for message in websocket:
                self._room.publish("global", ChatMessage(text=message.text))


class App(BaseApp):
    async def wire(self) -> None:
        room = Channel(ChatMessage, overflow="close", queue_size=64)
        self._include_websocket(ChatWebSocket(room))


app = App()
```

`publish` is synchronous and fire-and-forget. On a full queue, `"close"` closes that
consumer with `1013`; `"drop-oldest"` replaces stale queued data, which suits tickers.
The attachment context always removes the connection and its topic memberships.
`queue_size` must be a positive, non-boolean integer.

A channel is per process. With multiple workers, relay an external pub/sub subscription
into each worker's local channel:

```python
async def relay(channel: Channel[ChatMessage], broker: Broker) -> None:
    async for message in broker.messages():
        channel.publish("global", message)
```

jero deliberately does not choose Redis, PostgreSQL, NATS, or another backplane.

## Testing

The synchronous `TestClient` exposes the same typed contract:

```python
with client.websocket("/weather", inbound=Inbound, outbound=Outbound) as websocket:
    websocket.send(WeatherRequest(request_id="request-id", city="city"))
    response = websocket.receive()
```

An HTTP handshake rejection raises `WebSocketUpgradeError` with its `TestResponse` on
`.response`. A close raises `WebSocketClosedError`, whose `.code` and `.reason` are
available for assertions. `send_text` and `send_bytes` allow deliberate protocol-error
tests.
