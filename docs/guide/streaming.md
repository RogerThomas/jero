# Streaming

For long or open-ended responses, return one of three streaming wrappers instead of a
buffered body. Each is typed end-to-end — the generic `T` carries the per-item schema
(which the OpenAPI work will read), and an optional `H` carries typed response headers
just like the [buffered responses](responses.md).

| Wrapper                          | Content type            | Yields                       |
| -------------------------------- | ----------------------- | ---------------------------- |
| `StreamingResponse`              | `application/octet-stream` | `bytes` chunks            |
| `NDJSONStreamingResponse[T]`     | `application/x-ndjson`  | one `T` Struct per line      |
| `SSEResponse[T]`                 | `text/event-stream`     | Server-Sent Events (GET-only)|

## NDJSON

Stream one JSON `Struct` per line — ideal for large result sets a client consumes
incrementally:

```python
from collections.abc import AsyncIterator

from msgspec import Struct

from jero import BaseApp, Endpoint, NDJSONStreamingResponse


class Movie(Struct):
    title: str


class MoviesEndpoint(Endpoint):
    async def _movies(self) -> AsyncIterator[Movie]:
        for title in ("first", "second"):     # stream rows from a DB cursor, etc.
            yield Movie(title=title)

    async def get(self) -> NDJSONStreamingResponse[Movie]:
        return NDJSONStreamingResponse(stream=self._movies())


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(MoviesEndpoint(), path="/movies")


app = App()
```

## Server-Sent Events

`SSEResponse` is GET-only. Yield a `Struct`/`str` (sent as `data`) or a
`ServerSentEvent` to control the `event` / `id` / `retry` fields:

```python
from collections.abc import AsyncIterator

from msgspec import Struct

from jero import BaseApp, Endpoint, SSEResponse, ServerSentEvent


class Movie(Struct):
    title: str


class EventsEndpoint(Endpoint):
    async def _events(self) -> AsyncIterator[Movie | ServerSentEvent[Movie]]:
        yield Movie(title="first")                                    # data: {...}
        yield ServerSentEvent(data=Movie(title="second"), event="added", id="2")

    async def get(self) -> SSEResponse[Movie]:
        return SSEResponse(stream=self._events())


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(EventsEndpoint(), path="/events")


app = App()
```

### Keepalive

Set `keepalive` to emit a comment ping every N idle seconds, so proxies don't drop an
idle connection:

```python
SSEResponse(stream=self._events(), keepalive=15.0)   # ": ping" every 15s idle
```

## Raw bytes

For anything else — CSV, a proxied download — stream `bytes`:

```python
from collections.abc import AsyncIterator

from jero import BaseApp, Endpoint, StreamingResponse


class CsvEndpoint(Endpoint):
    async def _chunks(self) -> AsyncIterator[bytes]:
        yield b"id,name\n"
        yield b"1,gizmo\n"

    async def get(self) -> StreamingResponse:
        return StreamingResponse(stream=self._chunks(), raw_headers={"content-type": "text/csv"})


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(CsvEndpoint(), path="/export")


app = App()
```

## Setup & teardown (lifecycle)

A plain async iterable is enough when there's nothing to clean up. When the stream
holds a resource — a DB cursor, an upstream connection — give it a **one-yield
lifecycle generator**: yield the stream once, then do teardown after. The framework
guarantees the teardown runs, even on client disconnect or a mid-stream error:

```python
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator

from msgspec import Struct

from jero import BaseApp, Endpoint, NDJSONStreamingResponse


class Movie(Struct):
    title: str


class ExportEndpoint(Endpoint):
    async def _rows(self) -> AsyncIterator[Movie]:
        yield Movie(title="first")
        yield Movie(title="second")

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Movie]]:
        # open the resource (a DB cursor, an upstream connection) before streaming
        yield self._rows()
        # runs after the stream finishes — or is abandoned: close the resource here

    async def get(self) -> NDJSONStreamingResponse[Movie]:
        return NDJSONStreamingResponse(stream=self._lifecycle())


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(ExportEndpoint(), path="/movies/export")


app = App()
```

This is the one blessed way to scope a resource to a stream: a simple stream if you
don't need lifecycle, a one-yield generator if you do.

## Disconnect handling

jero watches the client connection while it streams. If the client disconnects, it
stops pulling from your iterator and runs the lifecycle teardown — you don't write any
of that bookkeeping. Errors raised inside the stream are swallowed after teardown so a
broken stream can't crash the worker.

## Status & headers

Streaming wrappers carry `status_code`, typed `headers`, and `raw_headers`, exactly
like the [buffered responses](responses.md). `HEAD` requests return the headers with no
body and never iterate the stream.
