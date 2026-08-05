"""jero's ``__call__`` against the ecosystem-standard ASGI types.

asgiref, Starlette and httpx all spell the ASGI triple over ``MutableMapping``, so an app
that narrows ``scope``/``receive`` to ``dict`` is un-assignable to their ``ASGIApp`` alias
even though it runs perfectly under every real server. That failure is invisible to the
rest of this suite (which passes real dicts) and surfaces only in a *user's* type checker,
when they mount jero behind something typed the standard way. The aliases below mirror the
ecosystem ones, and the assignment in the first test is the gate: the four checkers this
suite runs under fail if ``jero.core`` narrows those aliases again.

The second test keeps the widened annotation honest at runtime. A scope that is a
``MutableMapping`` but *not* a ``dict`` has to serve a real request, or the type is a
promise jero does not keep.
"""

import asyncio
from collections import UserDict
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Self

import pytest

from jero import BaseApp, Endpoint, Struct

# The ASGI triple as the ecosystem declares it (asgiref/Starlette/httpx), spelled out
# here rather than imported so the gate holds without depending on those packages.
type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class Pong(Struct):
    """The ping response body."""

    message: str


class PingEndpoint(Endpoint, path="/ping"):
    """One route, enough to drive a full request cycle."""

    async def get(self) -> Pong:
        """GET operation."""
        return Pong(message="pong")


class PingApp(BaseApp):
    """App wiring the ping endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(PingEndpoint())


class MappingScope(UserDict[str, Any]):
    """A scope satisfying ``MutableMapping`` without being a ``dict`` — precisely what the
    widened annotation claims jero accepts, and what no other test passes it."""


class Lifespan:
    """Drives the lifespan protocol over the ASGI interface, the way a server does, so the
    app wires itself without the test reaching for private methods."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._events: asyncio.Queue[Message] = asyncio.Queue()
        self._sent: asyncio.Queue[Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> Message:
        return await self._events.get()

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        await self._sent.put(message)

    async def _handshake(self, event: str) -> str:
        await self._events.put({"type": event})
        return str((await self._sent.get())["type"])

    async def _run(self) -> None:
        # A coroutine of its own: ``ASGIApp`` returns an ``Awaitable``, which is not a
        # ``Coroutine``, so the call cannot go straight into ``create_task``.
        await self._app(MappingScope({"type": "lifespan"}), self._receive, self._send)

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run())
        assert await self._handshake("lifespan.startup") == "lifespan.startup.complete"
        return self

    async def __aexit__(self, *_: object) -> None:
        assert await self._handshake("lifespan.shutdown") == "lifespan.shutdown.complete"
        assert self._task is not None
        await self._task


class Cycle:
    """The ``receive``/``send`` pair for one request, collecting what the app sends back."""

    def __init__(self) -> None:
        self.status = 0
        self.body = b""

    async def receive(self) -> Message:
        """The whole (empty) request body in one chunk."""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: MutableMapping[str, Any]) -> None:
        """Record the status off the start message, accumulate the body chunks."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
        else:
            self.body += message["body"]


def test_app_is_assignable_to_the_standard_asgi_app_type() -> None:
    """An app satisfies the ``MutableMapping``-based ``ASGIApp`` the ecosystem declares."""
    # The annotation *is* the assertion: this is what a user's type checker does when they
    # mount jero behind standard-typed middleware or httpx's ASGITransport.
    app: ASGIApp = PingApp()

    assert callable(app)


@pytest.mark.asyncio
async def test_app_serves_a_request_from_a_non_dict_mutable_mapping_scope() -> None:
    """The widened annotation is honest: a non-``dict`` mapping really does serve."""
    app = PingApp()
    cycle = Cycle()

    async with Lifespan(app):
        await app(
            MappingScope(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/ping",
                    "query_string": b"",
                    "headers": [],
                },
            ),
            cycle.receive,
            cycle.send,
        )

    assert cycle.status == 200
    assert cycle.body == b'{"message":"pong"}'
