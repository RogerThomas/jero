# WebSockets

Use a WebSocket when the protocol is genuinely bidirectional: the client and server
both initiate messages over one long-lived connection. For one-way server push, prefer
[Server-Sent Events](streaming.md#server-sent-events). SSE reconnects naturally, works
through ordinary HTTP infrastructure, and is usually the simpler contract.

jero WebSockets are typed in both directions. Framing, decoders, binding and
authentication are compiled at wiring. A handler only receives a connection whose
handshake has passed those checks.

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

What to notice:

- **Every member needs an explicit string tag**, even when a direction currently has one
  Struct (kebab-case is the convention). The tag is a deployed wire contract: adding a
  second member later must not change how the first is represented.
- **The final `assert_never` arm** makes adding an inbound member without handling it a
  static error.
- **Naming.** Correlated ask/answer messages conventionally use `XRequest` / `XResponse`
  and carry a `request_id: str` that the server echoes. Unsolicited server-push messages
  use plain names such as `Tick` or `Departed`.

## Handshake binding and authentication

`handle` may bind `path`, `params`, `headers`, `cookies`, `raw_headers`, and `user`
exactly like an HTTP handler. Mount an authenticator with `auth=` on
`_include_websocket`.

A browser's WebSocket API cannot set an `Authorization` header but always sends cookies,
so browser clients authenticate with [cookie auth](cookies.md#websockets-the-motivating-case).

Binding and authentication run before the upgrade. Only a valid handshake is accepted
and enters the handler, so acceptance is implicit: there is no `websocket.accept()`.

A failed handshake is rejected one of two ways:

- **The ASGI server advertises the WebSocket denial response extension:** the client
  gets the ordinary typed HTTP rejection, body included.
- **It does not:** jero sends a pre-accept close and the server rejects the upgrade,
  normally as HTTP 403 without the typed body.

## Closing

To refuse a client after the handshake, send a typed message, then close with a
`4000–4999` code:

```python
await websocket.send(RoomFull(retry_after_seconds=30))
await websocket.close(code=4003, reason="room full")
return
```

The close codes you will see:

| Code        | Sent when |
| ----------- | --------- |
| `1003`      | A frame of the wrong kind arrived (see [Framing](#framing)) |
| `1007`      | An inbound message was malformed JSON |
| `1008`      | An inbound message violated the schema |
| `1009`      | An inbound message was larger than `max_frame_size` |
| `1011`      | The handler raised an uncaught error |
| `1013`      | A [`Channel`](#broadcasting-with-channel) queue overflowed under `overflow="close"` |
| `4000–4999` | Yours: the application range |

Rules for `close()`:

- `code` must be one of the standard codes a server may send, or in `4000–4999`.
  `1010` is client-only, so it is rejected.
- `reason` is limited to the protocol's 123 UTF-8 bytes.
- An invalid code or reason raises before any close event is sent.
- Sending after the connection starts closing is rejected locally, so no invalid ASGI
  event is emitted.

## Limits

- **Frame size.** Inbound frames may be at most 1 MiB by default. Override this per
  mount with `max_frame_size=`.
- **Keepalive.** Ping/pong belongs to the ASGI server: standard ASGI does not expose
  ping frames to applications.

## Framing

Each direction chooses exactly one kind:

| Declared type | Text frame | Binary frame |
| --- | --- | --- |
| tagged Struct or tagged Struct union | strict JSON | close `1003` |
| `str` | text verbatim | close `1003` |
| `bytes` | close `1003` | bytes verbatim |

- The directions are independent, so `WebSocket[bytes, Transcript]` is valid.
- Mixed forms such as `Message | str` are not.
- Raw `str` and `bytes` are escape hatches for third-party JSON, msgpack, audio, or
  another protocol you own.

WebSockets are not represented by OpenAPI. The tagged message unions are the
client-facing contract. AsyncAPI generation is outside jero's current scope.

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

- `publish` is synchronous and fire-and-forget.
- `overflow` decides what a full queue does: `"close"` closes that consumer with `1013`,
  and `"drop-oldest"` replaces stale queued data, which suits tickers.
- `queue_size` must be a positive, non-boolean integer.
- Leaving the `attach` context always removes the connection and its topic memberships.

A channel is per process. With multiple workers, relay an external pub/sub subscription
into each worker's local channel:

```python
async def relay(channel: Channel[ChatMessage], broker: Broker) -> None:
    async for message in broker.messages():
        channel.publish("global", message)
```

jero does not choose Redis, PostgreSQL, NATS, or another backplane for you.

## Testing

The synchronous `TestClient` exposes the same typed contract:

```python
with client.websocket("/weather", inbound=Inbound, outbound=Outbound) as websocket:
    websocket.send(WeatherRequest(request_id="request-id", city="city"))
    response = websocket.receive()
```

- An HTTP handshake rejection raises `WebSocketUpgradeError`, with its `TestResponse` on
  `.response`.
- A close raises `WebSocketClosedError`, with `.code` and `.reason` to assert on.
- `send_text` and `send_bytes` send raw frames, for deliberate protocol-error tests.
