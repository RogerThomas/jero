# Responses & headers

A handler's return annotation is its response contract: jero reads it at startup, and
the [OpenAPI spec](openapi.md) is built from it. Return a plain value when you only need
a body, or a response wrapper when you also need headers or a status.

## Plain returns

| Return type    | Sent as                                    |
| -------------- | ------------------------------------------ |
| a `Struct`     | `application/json`                         |
| `list[Struct]` | `application/json` (a JSON array)          |
| `str`          | `text/plain; charset=utf-8`                |
| `bytes`        | `application/octet-stream`                 |

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, Resource


class Widget(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:      # JSON object
        return Widget(id=path.widget_id, name="name")

    async def read_many(self) -> list[Widget]:                 # JSON array
        return [Widget(id="widget-id", name="name")]


class ExportEndpoint(Endpoint, path="/export"):
    async def get(self) -> bytes:                              # octet-stream
        return b"id,name\n"


class HealthEndpoint(Endpoint, path="/healthz"):
    async def get(self) -> str:                                # text/plain
        return "ok"


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_endpoint(ExportEndpoint())
        self._include_endpoint(HealthEndpoint())


app = App()
```

- **A JSON body is always a `Struct`** (or a list of them), never a raw `dict`. A `dict`
  return is a `WiringError` at startup.
- **`str` is text, not raw JSON.** Returning `'{"status": "ok"}'` sends that literal text
  as `text/plain`.

## Controlling headers & status

To set headers or override the status, return a wrapper. Wrappers are generic, so the
body and header types stay in the signature:

```python
from msgspec import Struct

from jero import BaseApp, JSONResponse, Resource


class Widget(Struct):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetHeaders(Struct):
    x_cache: str
    x_rate_limit: int


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> JSONResponse[Widget, WidgetHeaders]:
        return JSONResponse(
            json=Widget(id=path.widget_id),
            headers=WidgetHeaders(x_cache="x-cache", x_rate_limit=100),
        )


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

| Wrapper               | Body             | Status         |
| --------------------- | ---------------- | -------------- |
| `JSONResponse[T, H]`  | `json: T`        | verb's default |
| `BytesResponse[H]`    | `content: bytes` | verb's default |
| `Created[T, H]`       | `json: T`        | 201            |
| `Accepted[T, H]`      | `json: T`        | 202            |
| `NoContent[H]`        | none             | 204            |

The type parameters:

- **`T` is the body**: a `Struct`, or a union of tagged Structs
  (`JSONResponse[Widget | Archived]`). Tag every member, as for a
  [mixed stream](streaming.md#mixed-streams).
- **`T` is required.** A bare `-> JSONResponse` is an error under a strict type checker,
  and once `_include_openapi` is wired it also fails at startup with
  `must name its body type`.
- **`H` is the typed headers Struct.** It defaults to `None`, so `JSONResponse[Widget]`
  is a body with no typed headers.

Every wrapper also takes `cookies`, `raw_headers`, `status_code`, and
[`location` / `links`](links-and-location.md).

## Headers

A response sets headers three ways. The typed/raw split mirrors how a handler
[receives](binding.md#headers-headers-typed-and-raw_headers-opaque) them.

| Field         | Type                             | Use it for                                |
| ------------- | -------------------------------- | ----------------------------------------- |
| `headers`     | your `Struct` (`H`)              | the conventional case                     |
| `cookies`     | `Sequence[SetCookie]`            | `Set-Cookie`                              |
| `raw_headers` | a mapping, or `RawHeaders`       | exact wire control: casing, underscores, a repeated header jero has no typed name for |

### Typed — `headers`

```python
class Headers(Struct):
    x_request_id: str
    x_rate_remaining: int
    x_debug: DebugInfo | None = None   # a Struct -> JSON string; None -> omitted


JSONResponse(json=widget, headers=Headers(x_request_id="x-request-id", x_rate_remaining=42))
# X-Request-Id: x-request-id
# X-Rate-Remaining: 42
```

- **Names**: the inverse of the request mapping, so `x_trace_id` is sent as
  `x-trace-id`.
- **Values** are sent as strings: scalars plainly (`bool` as `true` / `false`), nested
  Structs and lists as JSON.
- **`None`** fields are omitted.
- Typed headers are the ones the OpenAPI spec documents.

### Cookies — `cookies`

One `Set-Cookie` header per entry, secure by default:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import SetCookie

JSONResponse(json=widget, cookies=[SetCookie("session_id", token)])
```

See [Cookies](cookies.md) for `SetCookie`'s default attributes, deleting with
`SetCookie.expire`, and cookie auth.

### Raw — `raw_headers`

A plain mapping, or a `RawHeaders`. Pass a request's `RawHeaders` straight through to
forward it, repeats and all, or build one to repeat a header:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import RawHeaders

JSONResponse(
    json=widget,
    raw_headers=RawHeaders([("X-Trace-Id", "a"), ("X-Trace-Id", "b")]),
)
```

### Order and managed headers

- Headers are emitted in this order: typed `headers`, then one `Set-Cookie` per
  `cookies` entry, then `raw_headers` (so its own repeats survive).
- `content-type` defaults per kind.
- `content-length` is always set by jero; one you supply is ignored.

## Status codes

Without a wrapper, the status is the verb's default: 201 for `create`, else 200. To
answer with another one:

- **204, 201, 202**: return `NoContent[H]`, `Created[T, H]`, or `Accepted[T, H]`. They
  send their own status whatever the verb's default is.
- **Anything else**: set `status_code` on any wrapper. It overrides the verb's default
  and a wrapper's own status alike.

```python
from msgspec import Struct

from jero import Accepted, BaseApp, Endpoint, JSONResponse


class Job(Struct):
    id: str


class ImportResult(Struct):
    imported: int
    failed: int


class JobsEndpoint(Endpoint, path="/jobs"):
    async def post(self) -> Accepted[Job]:                 # 202
        return Accepted(json=Job(id="job-id"))


class ImportsEndpoint(Endpoint, path="/imports"):
    async def post(self) -> JSONResponse[ImportResult]:    # 207: no dedicated wrapper
        return JSONResponse(json=ImportResult(imported=1, failed=1), status_code=207)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(JobsEndpoint())
        self._include_endpoint(ImportsEndpoint())


app = App()
```

`NoContent` carries `headers`, `raw_headers`, `location`, and `links` like any response
(a 204 may carry a `Location`). At 204 neither `content-type` nor `content-length` is
sent, since the status forbids them.

`status_code` is also on the [streaming responses](streaming.md).

## Naming a response type

`JSONResponse[Widget, CacheHeaders]` gets repetitive across a dozen handlers. Give it a
name with a `type` alias, or by subclassing the wrapper:

```python
from dataclasses import dataclass, field

from msgspec import Struct

from jero import BaseApp, Endpoint, JSONResponse


class Widget(Struct):
    id: str
    name: str


class CacheHeaders(Struct):
    cache_control: str = "no-store"


type WidgetResponse = JSONResponse[Widget, CacheHeaders]      # an alias
type Envelope[T: Struct] = JSONResponse[T, CacheHeaders]      # generic: pin H, vary T


@dataclass(kw_only=True, slots=True)
class Cached(JSONResponse[Widget, CacheHeaders]):             # or a subclass
    """Same document, plus a default so callers need not pass the headers."""

    headers: CacheHeaders = field(default_factory=CacheHeaders)


class Aliased(Endpoint, path="/aliased"):
    async def get(self) -> WidgetResponse:
        return JSONResponse(json=Widget(id="widget-id", name="name"), headers=CacheHeaders())


class Enveloped(Endpoint, path="/enveloped"):
    async def get(self) -> Envelope[Widget]:
        return JSONResponse(json=Widget(id="widget-id", name="name"), headers=CacheHeaders())


class Subclassed(Endpoint, path="/subclassed"):
    async def get(self) -> Cached:
        return Cached(json=Widget(id="widget-id", name="name"))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(Aliased())
        self._include_endpoint(Enveloped())
        self._include_endpoint(Subclassed())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

All three document the same thing: one 200 whose schema refs `Widget`, with
`cache-control` in `headers`. The name you chose appears nowhere in the spec.

- **Alias or subclass?** An alias when you only want the name, a subclass when it
  should also carry defaults.
- Aliases of aliases resolve.
- Either form works as a union member (`WidgetResponse | NoContent`).
- A subclass may stay generic too: `class Envelope[T: Struct](JSONResponse[T, CacheHeaders])`,
  written `-> Envelope[Widget]`.
- The body type must still be named somewhere in the chain (see
  [above](#controlling-headers-status)). `SSEResponse` is the exception: its `T`
  defaults to `str`.

## Dynamic success status

A handler can answer with different success statuses by returning a **union**. Every
branch stays statically typed, and each gets its own entry in the OpenAPI spec:

```python
from msgspec import Struct

from jero import BaseApp, NoContent, Resource


class Widget(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget | NoContent:
        if path.widget_id.startswith("draft-"):
            return NoContent()                             # 204: exists, nothing to show
        return Widget(id=path.widget_id, name="name")      # 200 + Widget


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

The spec's `200` describes `Widget`, and its `204` has no body.

- **Any non-streaming return type can be a member**, plain returns included. A bare
  `Struct` needs no wrapper to sit beside a `NoContent`.
- **Each member keeps the status it would have alone.** A plain `Struct`,
  `list[Struct]`, `str`, `bytes`, `JSONResponse`, or `BytesResponse` takes the verb's
  default (201 for `create`, else 200). `NoContent` / `Created` / `Accepted` take their
  own.
- **Wrap a branch only when that branch needs it**, for example
  `JSONResponse[Widget, Headers] | NoContent` to add typed headers to the 200.
- **A union is for successes.** A widget that doesn't exist is a `404`, which `read_one`
  already documents because it has a `path` source. Failures stay
  [errors you raise](errors.md).

## Union reference

The rules for unions that go beyond the common case above.

### Members that share a status

Members may land on the same status. They merge into that one response:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, JSONResponse


class Widget(Struct, tag=True):
    id: str
    name: str


class Archived(Struct, tag=True):
    id: str
    archived_at: str


class CacheHeaders(Struct):
    cache_control: str = "no-store"


class RateHeaders(Struct):
    x_rate_limit: int = 100


class AcceptHeaders(Struct):
    accept: str = "application/json"


class MergedBodies(Endpoint, path="/merged-bodies"):
    async def get(self) -> Widget | Archived:                        # both 200
        return Archived(id="widget-id", archived_at="archived-at")


class MergedHeaders(Endpoint, path="/merged-headers"):
    async def get(
        self,
    ) -> JSONResponse[Widget, CacheHeaders] | JSONResponse[Archived, RateHeaders]:
        return JSONResponse(json=Widget(id="widget-id", name="name"), headers=CacheHeaders())


class Negotiated(Endpoint, path="/negotiated"):
    async def get(self, headers: AcceptHeaders) -> bytes | JSONResponse[Widget]:
        if headers.accept == "application/octet-stream":
            return b"raw bytes"
        return JSONResponse(json=Widget(id="widget-id", name="name"))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(MergedBodies())
        self._include_endpoint(MergedHeaders())
        self._include_endpoint(Negotiated())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

| Members at one status        | In the spec                                         | Example         |
| ---------------------------- | --------------------------------------------------- | --------------- |
| Different Struct bodies, same media type | one schema, `anyOf` of the members (plus a `discriminator` when they are tagged) | `MergedBodies`  |
| Different header Structs     | one header map with every member's headers          | `MergedHeaders` |
| Different media types        | both listed side by side under `content`            | `Negotiated`    |
| The same body                | one body, deduplicated                              | `BytesResponse[CacheHeaders] \| BytesResponse[RateHeaders]` |

- `-> Widget | Archived` and `-> JSONResponse[Widget | Archived]` produce the identical
  document. Pick whichever reads better.
- With one wrapper per member, the type checker enforces the pairing: in
  `MergedHeaders`, a `Widget` can't be returned with `RateHeaders`.

### What's rejected

| Union                                                         | Fails                                |
| ------------------------------------------------------------- | ------------------------------------ |
| A streaming member                                            | at startup                           |
| `-> Widget \| None` (return `NoContent`, not `None`, for a 204) | at startup                         |
| Members at one status whose header Structs give one wire name different types | at startup, once `_include_openapi` is wired |
| Members at one status and media type whose bodies differ but can't form an `anyOf`, such as a `list[Struct]` beside a `Struct` | at startup, once `_include_openapi` is wired |

For the last two, give the members distinct statuses, or one shared header Struct.

## Errors

Raise a typed `HTTPError` subclass from a handler to short-circuit with a Problem
Details response. See [Errors](errors.md) for static and parameterized errors, custom
error bodies, and exception handlers, and [REST semantics](rest.md) for the full
status-code map.
