#!yeet
"""In-process benchmark of the request hot path (decode -> handler -> encode).

Drives the app's ASGI interface directly — no socket, no server, no TestClient
threading — so the number isolates jero's own per-request work. The app is wired
through the real ASGI lifespan (public interface), then the POST path is hammered.

Run with ``yeet bench.py`` (or ``./bench.py``); ``--requests`` / ``--trials`` tune it.
Numbers are in-process, so coroutine-hop savings are amplified relative to a real
server where socket I/O dominates — treat deltas as directional, not absolute.
"""

import asyncio
import statistics
import time
from typing import Any

from msgspec import Struct
from msgspec.json import encode

from jero import BaseApp, Endpoint


class Movie(Struct):
    """A representative request/response body."""

    title: str
    year: int
    rating: float
    tags: list[str]
    director: str
    runtime_min: int
    language: str


class MoviesEndpoint(Endpoint):
    """Echoes the decoded body — exercises decode + handler call + encode."""

    async def post(self, json: Movie) -> Movie:
        """Echo the body back."""
        return json


class App(BaseApp):
    """The benchmark app: one POST endpoint."""

    async def _wire(self) -> None:
        self._include_endpoint(MoviesEndpoint(), path="/movies")


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


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": _BODY, "more_body": False}


async def _send(message: dict[str, Any]) -> None:
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


def main(requests: int = 200_000, trials: int = 7) -> None:
    """Hammer the POST hot path and report best/median req/s (best = least-interfered)."""
    rates = asyncio.run(_measure(requests, trials))
    best, med = max(rates), statistics.median(rates)
    print(f"requests={requests:,}  trials={trials}  body={len(_BODY)}B")
    print(f"best:   {best:>12,.0f} req/s   ({1e9 / best:6.0f} ns/req)")
    print(f"median: {med:>12,.0f} req/s   ({1e9 / med:6.0f} ns/req)")
