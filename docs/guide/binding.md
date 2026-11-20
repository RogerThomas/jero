# Request binding

Handler arguments bind **by name**. Declare only the ones you need, and jero fills
each from the request, validated against your `Struct`.

| Argument      | Source                          | Type                     |
| ------------- | ------------------------------- | ------------------------ |
| `json`        | request body (JSON)             | a `Struct`, or a [union of tagged `Struct`s](#one-of-several-shapes-a-tagged-union) |
| `content`     | request body (raw)              | `bytes`                  |
| `content_stream` | request body (raw, streamed) | `ContentStream`          |
| `form`        | form body (multipart, or url-encoded without files) | a `Struct` (see [Forms](forms.md)) |
| `params`      | query string                    | a `Struct`               |
| `path`        | URL template slots              | a `Struct`               |
| `headers`     | request headers                 | a `Struct`               |
| `cookies`     | request cookies                 | a `Struct` (see [Cookies](cookies.md)) |
| `raw_headers` | request headers (opaque)        | `RawHeaders`             |
| `user`        | the auth result                 | whatever `authenticate` returns (any type), or `T \| None` behind [optional auth](auth.md#optional-authentication) |

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

- **One request body.** `json`, `content`, `content_stream`, and `form` are mutually
  exclusive, and declaring one on a `GET` or `DELETE` handler is a `WiringError` at
  startup. Everything else combines freely.
- **Import the types normally**, never under `if TYPE_CHECKING:`. jero reads these
  annotations at startup (see the
  [FAQ](../faq.md#why-does-ruff-want-my-struct-imports-under-type_checking) if ruff
  suggests it).
- **Failures.** Malformed → **400**, schema failure → **422** (see
  [REST semantics](rest.md#status-codes)).

## JSON body — `json`

The body is decoded straight into your `Struct` by msgspec.

A JSON body is **always** a `Struct` (or a tagged union of them), never a raw `dict`.
That's what gives it both validation and a schema in the [OpenAPI spec](openapi.md).

### One of several shapes: a tagged union

When a body comes in more than one shape, declare `json` as a union of **tagged**
Structs, not one Struct with every field optional. Each member declares `tag=`
(`tag=True` uses the class name), and the handler narrows with `match` or `isinstance`.

```python
from typing import Annotated

from msgspec import Meta, Struct

from jero import BaseApp, Endpoint


class FixedDiscount(Struct, tag="fixed", tag_field="kind"):
    price_cents: int
    discount_cents: Annotated[int, Meta(ge=0)]


class PercentDiscount(Struct, tag="percent", tag_field="kind"):
    price_cents: int
    percent_off: Annotated[int, Meta(ge=0, le=100)]


type Discount = FixedDiscount | PercentDiscount


class PriceQuote(Struct):
    price_cents: int


class PriceQuotesEndpoint(Endpoint, path="/price-quotes"):
    async def post(self, json: Discount) -> PriceQuote:
        match json:
            case FixedDiscount():
                price_cents = json.price_cents - json.discount_cents
            case PercentDiscount():
                price_cents = json.price_cents * (100 - json.percent_off) // 100
        return PriceQuote(price_cents=max(price_cents, 0))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(PriceQuotesEndpoint())


app = App()
```

- `{"kind": "fixed", "price_cents": 1000, "discount_cents": 250}` decodes to a
  `FixedDiscount`.
- A missing tag, or one no member claims, is a **422** like any other schema failure.
- A union costs no more per request than a single Struct: msgspec reads the tag to pick
  the member.
- The `type` alias is optional (`json: FixedDiscount | PercentDiscount` works the same).
  An alias can itself sit inside a wider union (`json: Discount | Refund`).
- In the [OpenAPI spec](openapi.md) the body is an `anyOf` plus a `discriminator`
  mapping each tag to its member: the same schema a
  [tagged-union response](streaming.md#mixed-streams) gets.

The rules, all checked at startup:

- Every member is a `Struct` with an explicit **string** tag. An untagged member (msgspec
  couldn't tell it apart from the others) or an `int` tag is a `WiringError` naming the
  member.
- The members share one `tag_field` (default `"type"`), and no two claim the same tag.
- `tag_field` is spelled exactly as given: `rename` never touches it, so a
  `rename="camel"` member carries `kind` verbatim beside its camelCased fields.
- `json: X | None` is not an optional body. It's rejected.

`demo_app/operations/price_quote_operations.py` is the same endpoint in the
project-structured app, with camelCase fields.

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

jero sets no request body size limit, and `content` holds the whole body in memory. Cap
the size at your [server or reverse proxy](deployment.md#what-belongs-in-the-proxy), or read the body incrementally with
[`content_stream`](#streamed-raw-body-content_stream).

## Streamed raw body — `content_stream`

When a buffered body is the wrong shape (a large upload, a payload you proxy onward, a
format you parse incrementally), take `content_stream: ContentStream` and read the body
as it arrives. It is the escape hatch for any body jero has no typed vocabulary for.

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

Both are `Struct`s converted from strings (`?limit=5` → `limit: int = 5`).

- `params` fields may have defaults (optional query params).
- `path` fields may not (see [path templates](resources.md#path-templates)).
- Bad query → **400**; bad path value → **404**.

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

Use the typed `headers` Struct for values you act on, and `raw_headers` only to forward
the whole bag upstream or for diagnostics. The same split applies on the
[response side](responses.md#headers).

## Cookies — `cookies`

A typed `Struct`, like `headers`, with one difference: names bind **verbatim and
case-sensitive**. A field name *is* the cookie name, with no lower-casing and no `-` to
`_` mapping. For names that aren't valid identifiers (`__Host-session`), lenient
parsing, setting and deleting cookies, and cookie auth, see [Cookies](cookies.md).

## camelCase (and any wire convention)

msgspec's `rename` is honored everywhere. Define a base Struct for your wire
convention and inherit it: snake_case in code, camelCase on the wire.

```python
class Camel(Struct, rename="camel"):
    ...


class WidgetIn(Camel):
    price_cents: int           # decoded from {"priceCents": ...}
```
