#!yeet
"""In-process benchmark of the request hot path (decode -> handler -> encode).

Drives the app's ASGI interface directly — no socket, no server, no TestClient
threading — so the number isolates jero's own per-request work. The app is wired
through the real ASGI lifespan (public interface), then the POST path is hammered.

Run with ``yeet bench.py`` (or ``./bench.py``); positional ``REQUESTS`` / ``TRIALS`` tune it.
Numbers are in-process, so coroutine-hop savings are amplified relative to a real
server where socket I/O dominates — treat deltas as directional, not absolute.
"""

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

from msgspec import Struct
from msgspec.json import Decoder, Encoder, encode

from jero import BaseApp, Endpoint, WebSocket, WebSocketEndpoint


class Movie(Struct):
    """A representative request/response body."""

    title: str
    year: int
    rating: float
    tags: list[str]
    director: str
    runtime_min: int
    language: str


class MoviesEndpoint(Endpoint, path="/movies"):
    """Echoes the decoded body — exercises decode + handler call + encode."""

    async def post(self, json: Movie) -> Movie:
        """Echo the body back."""
        return json


class EchoMessage(Struct, tag="echo-message"):
    """The typed payload used in both WebSocket directions."""

    text: str


class EchoWebSocket(WebSocketEndpoint, path="/echo"):
    """Echo typed messages for the framework WebSocket benchmark."""

    async def handle(self, websocket: WebSocket[EchoMessage, EchoMessage]) -> None:
        """Decode and re-encode every inbound message."""
        async for message in websocket:
            await websocket.send(message)


class App(BaseApp):
    """The benchmark app: one POST endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(MoviesEndpoint())
        self._include_websocket(EchoWebSocket())


_BODY = encode(
    Movie(
        title="Inception",
        year=2010,
        rating=8.8,
        tags=["scifi", "thriller", "heist", "dream", "ensemble"],
        director="Christopher Nolan",
        runtime_min=148,
        language="en",
    )
)
_SCOPE: dict[str, Any] = {
    "type": "http",
    "method": "POST",
    "path": "/movies",
    "query_string": b"",
    "headers": [(b"content-type", b"application/json")],
}
_WEBSOCKET_TEXT = encode(EchoMessage(text="text")).decode()
_RAW_WEBSOCKET_DECODER = Decoder(EchoMessage)
_RAW_WEBSOCKET_ENCODER = Encoder()
_WEBSOCKET_SCOPE: dict[str, Any] = {
    "type": "websocket",
    "path": "/echo",
    "query_string": b"",
    "headers": [],
    "subprotocols": [],
}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": _BODY, "more_body": False}


async def _send(message: dict[str, Any]) -> None:
    _ = message


class _WebSocketCycle:
    """Feed a fixed number of frames to one benchmark connection."""

    def __init__(self, messages: int) -> None:
        self._remaining = messages
        self._connected = False

    async def receive(self) -> dict[str, Any]:
        """Produce connect, receive, then disconnect events."""
        if not self._connected:
            self._connected = True
            return {"type": "websocket.connect"}
        if self._remaining:
            self._remaining -= 1
            return {"type": "websocket.receive", "text": _WEBSOCKET_TEXT}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(self, message: dict[str, Any]) -> None:
        """Discard one benchmark response event."""
        _ = message


async def _measure(requests: int, trials: int) -> list[float]:
    app = App()

    # Wire the app via the real ASGI lifespan (keeps the lifespan task alive for the
    # run, then shuts it down cleanly), so we touch only the public interface.
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    started = asyncio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        return await to_app.get()

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    lifespan = asyncio.create_task(app({"type": "lifespan"}, lifespan_receive, lifespan_send))
    await to_app.put({"type": "lifespan.startup"})
    await started.wait()

    for _ in range(2000):  # warm up
        await app(_SCOPE, _receive, _send)

    rates: list[float] = []
    for _ in range(trials):
        start = time.perf_counter()
        for _ in range(requests):
            await app(_SCOPE, _receive, _send)
        rates.append(requests / (time.perf_counter() - start))

    await to_app.put({"type": "lifespan.shutdown"})
    await lifespan
    return rates


async def _measure_websocket(messages: int, trials: int) -> list[float]:
    app = App()
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    started = asyncio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        return await to_app.get()

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    lifespan = asyncio.create_task(app({"type": "lifespan"}, lifespan_receive, lifespan_send))
    await to_app.put({"type": "lifespan.startup"})
    await started.wait()
    rates: list[float] = []
    for _ in range(trials):
        cycle = _WebSocketCycle(messages)
        start = time.perf_counter()
        await app(_WEBSOCKET_SCOPE, cycle.receive, cycle.send)
        rates.append(messages / (time.perf_counter() - start))
    await to_app.put({"type": "lifespan.shutdown"})
    await lifespan
    return rates


async def _raw_websocket(
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    _ = scope
    await receive()
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        decoded = _RAW_WEBSOCKET_DECODER.decode(message["text"])
        await send(
            {"type": "websocket.send", "text": _RAW_WEBSOCKET_ENCODER.encode(decoded).decode()}
        )


async def _measure_raw_websocket(messages: int, trials: int) -> list[float]:
    rates: list[float] = []
    for _ in range(trials):
        cycle = _WebSocketCycle(messages)
        start = time.perf_counter()
        await _raw_websocket(_WEBSOCKET_SCOPE, cycle.receive, cycle.send)
        rates.append(messages / (time.perf_counter() - start))
    return rates


def main(requests: int = 200_000, trials: int = 7) -> None:
    """Hammer the POST hot path and report best/median req/s (best = least-interfered)."""
    rates = asyncio.run(_measure(requests, trials))
    best, med = max(rates), statistics.median(rates)
    print(f"requests={requests:,}  trials={trials}  body={len(_BODY)}B")
    print(f"best:   {best:>12,.0f} req/s   ({1e9 / best:6.0f} ns/req)")
    print(f"median: {med:>12,.0f} req/s   ({1e9 / med:6.0f} ns/req)")
    websocket_rates = asyncio.run(_measure_websocket(requests, trials))
    websocket_best = max(websocket_rates)
    websocket_med = statistics.median(websocket_rates)
    raw_websocket_med = statistics.median(asyncio.run(_measure_raw_websocket(requests, trials)))
    print(f"websocket best:   {websocket_best:>12,.0f} msg/s")
    print(f"websocket median: {websocket_med:>12,.0f} msg/s")
    print(
        f"raw typed ASGI:   {raw_websocket_med:>12,.0f} msg/s   "
        f"({raw_websocket_med / websocket_med:.2f}x jero)"
    )
