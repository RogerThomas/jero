"""The ``content_stream`` binding source: the raw request body, read incrementally.

The escape hatch for any body jero has no typed vocabulary for (a large upload, a
proxied payload, a format you parse yourself). Nothing is buffered: each chunk is
pulled from the ASGI server as the handler asks for it.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

type _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]


class ClientDisconnectedError(Exception):
    """The client went away before the request body was fully received.

    Raised from a :class:`ContentStream` iterator. jero answers nothing for the request
    (there is no one left to answer), so a handler only catches it to clean up partial
    work, then lets it propagate.
    """


@dataclass(slots=True)
class ContentStream:
    """The request body as an async stream of ``bytes`` chunks; read it at most once.

    ``async for chunk in content_stream`` yields the chunks exactly as the server
    delivers them; :meth:`iter_chunks` re-frames them to a fixed size. A client that
    disconnects mid-body surfaces as :class:`ClientDisconnectedError`.
    """

    _receive: _Receive
    _claimed: bool = field(default=False, init=False)
    _done: bool = field(default=False, init=False)
    _disconnected: bool = field(default=False, init=False)
    # Created only when the response side waits on an unfinished body (see
    # ``receive_after_body``), so the common read-then-respond request never pays for it.
    _finished: asyncio.Event | None = field(default=None, init=False)

    def _claim(self) -> None:
        if self._claimed:
            raise RuntimeError("content_stream can only be read once")
        self._claimed = True

    def _finish(self, *, disconnected: bool) -> None:
        self._done = True
        self._disconnected = disconnected
        if self._finished is not None:
            self._finished.set()

    async def _chunks(self) -> AsyncIterator[bytes]:
        while True:
            message = await self._receive()
            if message["type"] == "http.disconnect":
                self._finish(disconnected=True)
                raise ClientDisconnectedError()
            chunk: bytes = message.get("body", b"")
            more = bool(message.get("more_body"))
            if not more:
                self._finish(disconnected=False)
            if chunk:
                yield chunk
            if not more:
                return

    async def _sized(self, chunk_size: int) -> AsyncIterator[bytes]:
        buffer = bytearray()
        async for chunk in self._chunks():
            buffer += chunk
            while len(buffer) >= chunk_size:
                yield bytes(buffer[:chunk_size])
                del buffer[:chunk_size]
        if buffer:
            yield bytes(buffer)

    def __aiter__(self) -> AsyncIterator[bytes]:
        self._claim()
        return self._chunks()

    def iter_chunks(self, chunk_size: int) -> AsyncIterator[bytes]:
        """The body re-framed into ``chunk_size``-byte chunks (the last may be shorter)."""
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        self._claim()
        return self._sized(chunk_size)

    async def receive_after_body(self) -> MutableMapping[str, Any]:
        """The ASGI ``receive`` for jero's response side on a ``content_stream`` route.

        A streaming response watches ``receive`` for a disconnect while it streams; if
        its source is still reading this body, both would pull from the server and the
        watcher would swallow body chunks. So the watcher waits here until the body is
        finished (fully read, or the client went away) and only then reads on.
        """
        if not self._done:
            if self._finished is None:
                self._finished = asyncio.Event()
            await self._finished.wait()
        if self._disconnected:
            return {"type": "http.disconnect"}
        return await self._receive()
