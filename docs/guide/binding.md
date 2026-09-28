# Request binding

Handler arguments bind **by name**. You declare only the ones you need; jero resolves
each from the request and validates it against your `Struct` — once at startup it
learns *which* sources a handler wants, and the request path just fills them in.

| Argument      | Source                          | Type                     |
| ------------- | ------------------------------- | ------------------------ |
| `json`        | request body (JSON)             | a `Struct`               |
| `content`     | request body (raw)              | `bytes`                  |
| `content_stream` | request body (raw, streamed) | `ContentStream`          |
| `form`        | form body (multipart, or url-encoded without files) | a `Struct` (see [Forms](forms.md)) |
| `params`      | query string                    | a `Struct`               |
| `path`        | URL template slots              | a `Struct`               |
| `headers`     | request headers                 | a `Struct`               |
| `cookies`     | request cookies                 | a `Struct` (see [Cookies](cookies.md)) |
| `raw_headers` | request headers (opaque)        | `RawHeaders`             |
| `user`        | the auth result                 | a `Struct`, or `Struct \| None` behind [optional auth](auth.md#optional-authentication) |

`json`, `content`, `content_stream`, and `form` are mutually exclusive (one request
body), and are rejected on bodyless verbs (`GET`, `DELETE`). Everything else can combine freely.

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


class WidgetPath(Struct):
    widget_id: str


class Page(Struct):
    limit: int = 20
    offset: int = 0


class WidgetResource(Resource, path="/widgets"):
    # PUT /widgets/{widget_id}?limit=...&offset=...
    async def update_full(self, path: WidgetPath, params: Page, json: WidgetIn) -> Widget:
        return Widget(id=path.widget_id, name=json.name)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

## JSON body — `json`

The body is decoded straight into your `Struct` by msgspec. A malformed body → **400**;
a well-formed body that fails the schema → **422**. That split runs through every
source: failures *reading* the request (malformed JSON, an unconvertible query or
header value) are 400s, and only a successfully parsed body or form part whose content
breaks its declared schema is a 422 — which is why a bad query value is a 400 where the
same bad value inside a JSON body is a 422.

A JSON body is **always** a `Struct`, never a raw `dict`. That's what gives it both
validation and a schema in the [OpenAPI spec](openapi.md).

## Raw body — `content`

For non-JSON or opaque bodies, take `content: bytes`:

```python
from msgspec import Struct

from jero import BaseApp, Resource


class Receipt(Struct):
    size: int


class UploadResource(Resource, path="/uploads"):
    async def create(self, content: bytes) -> Receipt:   # POST /uploads
        return Receipt(size=len(content))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(UploadResource())


app = App()
```

## Streamed raw body — `content_stream`

`content` holds the whole body in memory. When that's the wrong shape (a large upload,
a payload you proxy onward, a format you parse incrementally), take
`content_stream: ContentStream` instead and read the body as it arrives. It's the final
escape hatch: anything jero has no typed vocabulary for can still be read, one chunk at
a time.

```python
import hashlib

from msgspec import Struct

from jero import BaseApp, ContentStream, Endpoint


class Digest(Struct):
    size: int
    sha256: str


class UploadsEndpoint(Endpoint, path="/uploads"):
    async def post(self, content_stream: ContentStream) -> Digest:
        digest = hashlib.sha256()
        size = 0
        async for chunk in content_stream:
            digest.update(chunk)
            size += len(chunk)
        return Digest(size=size, sha256=digest.hexdigest())


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(UploadsEndpoint())


app = App()
```

Plain iteration yields the chunks exactly as the server delivers them, so their sizes are
the server's choice. To control the size, iterate `iter_chunks(chunk_size=n)` instead:
it re-frames the body into `n`-byte chunks, with a shorter final one.

```python
# doc-example: fragment
class UploadsEndpoint(Endpoint, path="/uploads"):
    _chunk_size: int = 64 * 1024

    async def post(self, content_stream: ContentStream) -> Digest:
        digest = hashlib.sha256()
        size = 0
        async for chunk in content_stream.iter_chunks(chunk_size=self._chunk_size):
            digest.update(chunk)
            size += len(chunk)
        return Digest(size=size, sha256=digest.hexdigest())
```

- Iteration is `async for`, so the handler must be `async def`; a sync handler taking
  `content_stream` is a `WiringError` at startup.
- The body can be read **once**. A second read raises `RuntimeError`.
- Auth and every other source bind before the handler runs, so a rejected request never
  reads its body.
- If the client disconnects partway through, the iterator raises
  `ClientDisconnectedError`. jero sends nothing (no one is left to answer) and logs
  nothing. Catch it only to clean up partial work, then let it propagate. It can't be
  given an [exception handler](errors.md).
- A [streaming response](streaming.md) can read `content_stream` as it streams (an
  echo, or a transform on the fly).
- The OpenAPI spec documents it exactly like `content`: an `application/octet-stream`
  binary body.

## Query & path — `params`, `path`

Both are `Struct`s converted from strings (`?limit=5` → `limit: int = 5`). `params`
fields may have defaults (optional query params); `path` fields may not (see
[path templates](resources.md#path-templates)). Bad query → **400**; bad path value →
**404**.

## Headers — `headers` (typed) and `raw_headers` (opaque)

For the conventional case, model the headers you act on as a typed `Struct`. Wire
names map to fields by lower-casing and turning `-` into `_`:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint


class Trace(Struct):
    x_trace_id: str            # reads the "X-Trace-Id" header
    user_agent: str | None = None


class TraceEcho(Struct):
    trace_id: str


class TraceEndpoint(Endpoint, path="/trace"):
    async def get(self, headers: Trace) -> TraceEcho:    # GET /trace
        return TraceEcho(trace_id=headers.x_trace_id)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(TraceEndpoint())


app = App()
```

When you need the headers exactly as sent — original casing, repeats, or names that
aren't valid identifiers — take `raw_headers: RawHeaders`. It's an immutable,
case-insensitive `Mapping` that preserves every pair:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, RawHeaders


class Echo(Struct):
    trace_id: str
    cookie_count: int


class EchoEndpoint(Endpoint, path="/echo"):
    async def get(self, raw_headers: RawHeaders) -> Echo:    # GET /echo
        trace_id = raw_headers["X-Trace-Id"]         # case-insensitive lookup
        cookies = raw_headers.getlist("Cookie")      # repeats preserved
        return Echo(trace_id=trace_id, cookie_count=len(cookies))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(EchoEndpoint())


app = App()
```

Use the typed `headers` Struct for values you act on; reach for `raw_headers` only for
forwarding the whole bag upstream or for diagnostics. The same split applies on the
[response side](responses.md#headers).

## Cookies — `cookies`

A typed `Struct`, bound the same way — but **verbatim and case-sensitive**, with no
header-style mangle at all: RFC 6265 cookie names are case-sensitive and routinely
aren't valid Python identifiers (`__Host-session`), so a field name simply *is* the
cookie name it binds. Full treatment — the rename idiom, lenient-parsing rules, setting
and deleting cookies, and cookie auth — lives in its own page: [Cookies](cookies.md).

## camelCase (and any wire convention)

msgspec's `rename` is honored everywhere. Define a base Struct for your wire
convention and inherit it — snake_case in code, camelCase on the wire:

```python
class Camel(Struct, rename="camel"):
    ...


class WidgetIn(Camel):
    price_cents: int           # decoded from {"priceCents": ...}
```
