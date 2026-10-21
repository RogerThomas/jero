"""A minimal in-process ASGI driver for the CodSpeed benchmarks.

Every request is driven straight through the app's ASGI callable on one event loop owned
by the benchmark — no server, no sockets, no helper thread — so the measurement is jero's
own per-request work: routing, binding/validation, the handler, and serialization.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from jero import BaseApp


class UnexpectedStatusError(Exception):
    """A benchmarked request answered with a status other than the expected one."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected status {expected}, got {actual}")


class LifespanError(Exception):
    """The app did not complete its lifespan startup or shutdown."""


@dataclass(frozen=True)
class Request:
    """One fixed HTTP request, with its ASGI scope built once up front."""

    method: str
    path: str
    query: bytes = b""
    headers: Sequence[tuple[bytes, bytes]] = ()
    body: bytes = b""
    scope: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": self.method,
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query,
            "root_path": "",
            "headers": [(b"host", b"bench"), *self.headers],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 80),
        }
        object.__setattr__(self, "scope", scope)


@dataclass
class _Cycle:
    """The receive/send pair for one request; records the response status."""

    _body: bytes
    _delivered: bool = False
    status: int = 0

    async def receive(self) -> dict[str, Any]:
        """Deliver the whole body in one message, then report a disconnect."""
        if self._delivered:
            return {"type": "http.disconnect"}
        self._delivered = True
        return {"type": "http.request", "body": self._body, "more_body": False}

    async def send(self, message: dict[str, Any]) -> None:
        """Record the status; the body itself is discarded."""
        if message["type"] == "http.response.start":
            self.status = message["status"]


@dataclass
class _Lifespan:
    """Drive an app's ASGI lifespan protocol from the outside."""

    _app: BaseApp[Any]
    _to_app: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue[dict[str, Any]])
    _from_app: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue[dict[str, Any]])
    _task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._from_app.put(message)

    async def _step(self, message_type: str) -> None:
        await self._to_app.put({"type": f"lifespan.{message_type}"})
        reply = await self._from_app.get()
        if reply["type"] != f"lifespan.{message_type}.complete":
            raise LifespanError(f"lifespan {message_type} failed: {reply}")

    async def startup(self) -> None:
        """Start the app's lifespan task and wait for wiring to complete."""
        self._task = asyncio.ensure_future(
            self._app({"type": "lifespan"}, self._receive, self._send)
        )
        await self._step("startup")

    async def shutdown(self) -> None:
        """Close the app's resources and wait for its lifespan task to finish."""
        await self._step("shutdown")
        if self._task is not None:
            await self._task


async def start_and_stop(app: BaseApp[Any]) -> None:
    """Run one full lifespan: wiring (and the OpenAPI build, if included), then shutdown."""
    lifespan = _Lifespan(app)
    await lifespan.startup()
    await lifespan.shutdown()


@dataclass
class Harness:
    """A started app plus the event loop that drives it."""

    _app: BaseApp[Any]
    _loop: asyncio.AbstractEventLoop
    _lifespan: _Lifespan

    @classmethod
    def start(cls, app: BaseApp[Any]) -> Self:
        """Create a fresh event loop and run the app's lifespan startup on it."""
        loop = asyncio.new_event_loop()
        lifespan = _Lifespan(app)
        loop.run_until_complete(lifespan.startup())
        return cls(app, loop, lifespan)

    async def _send_many(self, request: Request, repeat: int, expected: int) -> None:
        for _ in range(repeat):
            cycle = _Cycle(request.body)
            await self._app(dict(request.scope), cycle.receive, cycle.send)
            if cycle.status != expected:
                raise UnexpectedStatusError(expected, cycle.status)

    def send(self, request: Request, *, repeat: int = 1, expected: int = 200) -> None:
        """Send ``request`` ``repeat`` times, failing on any unexpected status."""
        self._loop.run_until_complete(self._send_many(request, repeat, expected))

    def close(self) -> None:
        """Shut the app down and close the loop."""
        try:
            self._loop.run_until_complete(self._lifespan.shutdown())
        finally:
            self._loop.close()
