# Plan: WebSockets (typed sockets and Channel fan-out)

Status: **built.** The surface, framing rules, handshake semantics, typed test
harness, and the `Channel` primitive ship as designed below.

## Goal

Typed, compiled WebSockets that inherit jero's invariants: everything about a
socket that can be known at wiring is resolved at wiring, both directions of the
wire are msgspec-validated Structs by default, and the per-message hot path is a
prebuilt decoder plus the user's handler and nothing else.

Positioning (this goes in the docs, and it is the FAQ answer until built):
server→client push is already first-class via SSE/NDJSON — WebSockets are for
**genuinely bidirectional** protocols. Most "we need websockets" cases are SSE
cases; the guide says so explicitly.

The surface is four names: `WebSocketEndpoint`, `WebSocket[In, Out]`,
`_include_websocket`, `Channel[T]`.

## Public API

```python
# client → server
class WeatherRequest(Struct, tag="weather-request", rename="camel"):
    request_id: str
    city: str

type Inbound = WeatherRequest | ...

# server → client
class WeatherResponse(Struct, tag="weather-response", rename="camel"):
    request_id: str
    city: str
    temperature_c: float
    conditions: str

type Outbound = WeatherResponse | ...


@dataclass
class TriviaWebSocket(WebSocketEndpoint, path="/trivia/{client_id}"):
    _weather_service: WeatherService

    async def handle(self, websocket: WebSocket[Inbound, Outbound], path: TriviaPath) -> None:
        async for message in websocket:
            match message:
                case WeatherRequest(request_id=rid, city=city):
                    report = await self._weather_service.for_city(city)
                    await websocket.send(WeatherResponse(request_id=rid, ...))
                case _:
                    assert_never(message)


class App(BaseApp[Factory]):
    async def wire(self) -> None:
        self._include_websocket(
            TriviaWebSocket(self._factory.weather_service()), auth=self._factory.auth()
        )
```

- `WebSocketEndpoint` mounts with the same `{slot}` path templates as `Endpoint`;
  routing gets a websocket table beside the HTTP verb tables.
- `handle` declares its sources like any handler: `path`/`params`/`headers`/`user`
  bind with the existing compiled sources machinery at the handshake.
- The socket is an async iterator of decoded, validated inbound messages;
  `socket.send(...)` encodes outbound. Iteration ends when the client is gone —
  code after the loop is the natural "on disconnect" spot.
- Docs idiom: `match`/`case` with a final `assert_never(message)` arm, so adding
  a union member without handling it is a *static* error. Class patterns with
  captures (`case WeatherRequest(request_id=rid):`) are shown with a one-line
  callout that pattern-side `attr=name` is extraction, not assignment — every
  reader hits that.
- Naming conventions (docs): correlated ask/answer pairs are `XRequest` /
  `XResponse` with a `request_id: str` the server echoes (clients multiplex on
  it); server-push messages get plain names (`Tick`, not `TickResponse`).
  Injected services are singular: `_weather_service: WeatherService`.

## Handshake semantics

DECIDED — **accept is implicit.** A WebSocket handshake is a request; jero
treats it as one. Auth runs against the handshake headers, path/query/header
Structs bind and validate, and only if everything passes does the framework send
the ASGI accept and enter `handle`. A handler that runs is a socket that is
open. There is no `await socket.accept()`: the reasons other frameworks make it
explicit (reject-before-upgrade, subprotocol choice) are wiring-time or compiled
concerns here, and forgetting-to-accept ceases to be a representable bug.

Two rejection paths, split by who is being told what:

- **Trust/protocol failures** (auth failure, path that doesn't bind, malformed
  handshake) → rejected **before upgrade** with the ordinary HTTP error when the
  server advertises ASGI's optional denial-response extension, otherwise with a
  pre-accept close (normally surfaced by the server as a bodyless HTTP 403). No socket
  is established; no upgrade is spent on an untrusted client.
- **Application-state rejections** ("room full", duplicate session) → `handle`
  accepts, optionally sends one typed farewell message (part of `Outbound`,
  e.g. `RoomFull(retry_after_seconds=30)`), and closes with an application
  close code. DECIDED: this is deliberate, not a workaround — the browser
  WebSocket API hides HTTP rejection details from page script but exposes close
  code/reason, so post-accept close is the only channel a browser client can
  actually read the "why" on.

DECIDED — close-code conventions: `4000–4999` is the application range
(`4003 room-full` style, documented); protocol violations use the standard
codes (`1003` unsupported data, `1007`/`1008` for invalid payloads, `1011` for
internal error, `1013` try-again-later for overflow policy `close`).

DECIDED — an uncaught exception in `handle` is logged through the existing
`jero` logger funnel and closes `1011`. Custom exception handlers do **not**
apply to sockets in v1 (`ExceptionResponse` is HTTP-shaped); recoverable errors
belong in the `Outbound` union as typed messages.

DECIDED — middleware `intercept` sees handshakes (they are requests); the
middleware plan's verb scoping applies. `observe` for sockets is out of scope
for v1.

## Framing: `WebSocket[In, Out]`

DECIDED — each direction declares **exactly one** payload kind:

| kind | text frames | binary frames |
| :-- | :-- | :-- |
| tagged Struct union | JSON, strictly decoded/encoded | close `1003` |
| `str` | raw text, verbatim | close `1003` |
| `bytes` | close `1003` | raw bytes, verbatim |

Nine combinations, each direction independent (`WebSocket[bytes, Union]` is the
audio-in/transcripts-out shape; `WebSocket[str, str]` is a raw text protocol;
`WebSocket[bytes, bytes]` is the full escape hatch). **No mixed unions in v1** —
`Union | str` / `Union | bytes` are a `WiringError`. Widening later is
backwards-compatible; narrowing never is, so v1 starts narrow and needs no
frame-dispatch rules at all.

DECIDED — wiring rules, fail-loud as ever:

- **Explicit tags on every socket Struct**, even a single-member union. msgspec
  wouldn't need the tag for one member, but an untagged protocol breaks every
  deployed client the day a second message type is added. Tag values are wire
  contract, decoupled from class names (kebab-case strings, the `HTTPError.type`
  argument).
- **An undeclared frame kind is an error, not a shrug** — the type parameter is
  the accepted-frames contract.
- **Malformed frames on a typed socket fail loud** (typed close, standard code).
  No try-decode-fall-back-to-something: silently reclassifying broken input is
  the failure mode typed sockets exist to kill.

DECIDED — `str`/`bytes` sockets are **user-owned payloads**: jero never parses
them. This is the same escape-hatch idiom as `content: bytes`/`raw_headers` on
the HTTP side. A user speaking msgpack brings `msgspec.msgpack.Decoder` in their
own handler (two lines, full typing recovered); a user stuck with an untagged
third-party JSON protocol brings their own dispatch (or pydantic's
`TypeAdapter` — their code, their call).

DECIDED — no structural/untagged union support in v1. msgspec deliberately has
no smart-union shape-sniffing (unpredictable with overlapping shapes, slower),
and jero inherits that stance. Future path if real demand appears: an explicit
opt-in (`Untagged[A | B]`) compiled as an **ordered chain** of prebuilt
decoders — deterministic by declaration order, first success wins, none →
protocol error. Not built until someone brings the use case.

## `Channel[T]` — broadcast

The broadcast primitive *is* the websocket feature; single-socket echo is table
stakes. `Channel` is an in-process, typed fan-out hub, built in the factory and
injected like any dependency (no module-level `ConnectionManager` globals):

```python
class Factory(BaseFactory):
    def room(self) -> Channel[Outbound]:
        return Channel(Outbound, overflow="close", queue_size=64)


@dataclass
class ChatWebSocket(WebSocketEndpoint, path="/chat/{client_id}"):
    _room: Channel[Outbound]

    async def handle(self, websocket: WebSocket[Inbound, Outbound], path: ChatPath) -> None:
        async with self._room.attach(websocket) as subscription:
            subscription.join(topic="global")
            async for message in websocket:
                ...
                self._room.publish("global", ChatMessage(...))
        self._room.publish("global", Departed(client_id=path.client_id))
```

DECIDED:

- **Encode once, send N times.** `publish` runs one msgspec encode and hands the
  same bytes frame to every subscriber — the difference between one ~µs encode
  and N of them per event.
- **Bounded queue + writer task per socket.** `publish` is synchronous
  fire-and-forget: it enqueues and returns, never awaiting a client. A slow
  consumer fills only its own queue and hits the **overflow policy declared at
  construction**: `"close"` (close `1013`) or `"drop-oldest"` (tickers, where
  stale frames are worthless). Nothing per-message is configurable.
- No delivery counts or publish acks — observability is a separate concern.
- Topics are plain `str` routing keys. `attach` is a context manager scoped to
  the connection; detach on any exit is lifecycle-guaranteed (the FastAPI
  chat-example bugs — sequential broadcast stalls, dead sockets breaking
  broadcasts mid-loop, leaked connections on non-disconnect exceptions — are
  unrepresentable).

DECIDED — **`Channel` is per-process; the backplane is the user's.** Granian
runs multiple workers, and cross-worker broadcast needs pub/sub infrastructure
jero refuses to pick (core stays msgspec-only). Every backend reduces to a
documented ten-line relay pattern — a background task consuming Redis/Postgres
LISTEN-NOTIFY/NATS and calling `channel.publish(...)` locally. jero owns the
part that is actually performance-critical (local fan-out to thousands of
sockets); the easy half stays out of core.

## Boundaries

- **OpenAPI does not model WebSockets** (AsyncAPI does; out of scope). The docs
  state the boundary; the tagged unions are the client-facing contract.
- **SSE stays the recommendation for one-way push** — the guide says when *not*
  to use a socket.

## Testing

A typed test socket client on `TestClient` is in-scope and is a large fraction
of the work (jero's testing bar, not an afterthought):

```python
with client.websocket("/trivia/abc", inbound=Inbound, outbound=Outbound) as websocket:
    websocket.send(WeatherRequest(request_id="request-id", city="city"))
    response = websocket.receive()          # decoded Outbound, typed
```

Must cover: handshake auth pass/reject (pre-upgrade status visible to tests),
binding failures, typed frame round-trips, protocol-error closes with code
assertions, `Channel` fan-out and overflow policies, disconnect teardown.
`demo_app/` gains a socket (the living example, per project convention).

## Performance notes

Per-message hot path: one prebuilt `Decoder(Inbound)` decode (tagged dispatch is
O(1) in C), the handler, one encode per send. Handshake cost is the existing
compiled HTTP machinery. The framing contract and typed decoder are precomputed
at wiring. `Channel.publish`
is one encode + N queue puts. Benchmark at build time with the
in-process harness (a websocket scenario added to `bench.py`): target is that
jero's per-message overhead over a raw ASGI websocket echo stays within the
same ~2× envelope as the HTTP path.

Validated 2026-08-05 (200k messages × 7 trials): jero median 1.32m msg/s versus
2.63m msg/s for an equivalent raw ASGI loop using the same prebuilt typed decoder
and encoder — 1.98×, inside the target envelope.

## V1 decisions and deferred work

1. `Channel.queue_size` defaults to 64. Ping/pong keepalive is delegated to the
   ASGI server because standard ASGI exposes no ping-frame API.
2. Inbound frames default to a 1 MiB maximum, configurable per mount; oversized
   frames close with `1009` (message too big).
3. **Full-duplex handlers** — v1 is receive-driven (`async for`) plus `Channel`
   writer tasks, which covers push+ask/answer. A handler that needs its own
   concurrent send loop (not via `Channel`) needs a task-group escape hatch;
   design pass deferred.
4. Subprotocol negotiation is deferred from v1.
5. `Channel.attach(websocket)` returns an async context-managed subscription;
   `join(topic)` and `leave(topic)` are synchronous.

## Staged build order

1. **Transport core**: websocket scope routing (`_include_websocket`, path tables),
   handshake binding + auth reuse, implicit accept, `WebSocket[In, Out]` with the
   nine framing combinations, close-code mapping, uncaught-exception → `1011`.
2. **Test client**: `client.websocket(...)` typed harness (nothing else is
   verifiable to jero's standards without it); first demo_app socket.
3. **`Channel`**: attach/join/leave, encode-once publish, per-socket writer
   tasks, overflow policies; fan-out tests incl. slow-consumer behavior.
4. **Hardening**: max frame size and server-owned keepalive behavior,
   benchmark scenario in the in-process harness.
5. **Docs**: `docs/guide/websockets.md` (SSE-first guidance, the framing table,
   ask/answer + correlation idiom, the FastAPI chat-example comparison, the
   backplane relay pattern; every example a complete runnable app), AGENTS.md
   public surface, FAQ update ("designed → built").
