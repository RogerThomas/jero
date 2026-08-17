# Responses & headers

What a handler returns is part of its type signature, so the response schema is known
at startup (and to the [OpenAPI spec](openapi.md)). There are two levels: return a plain
value when you just want a body, or a response wrapper when you want to control
headers or status.

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
        return Widget(id=path.widget_id, name="gizmo")

    async def read_many(self) -> list[Widget]:                 # JSON array
        return [Widget(id="widget-id", name="gizmo")]


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

A JSON body is **always** a `Struct` (or a list of them) — never a raw `dict`. A
`dict`/blob return is a `WiringError` at startup. That's the rule that gives every
endpoint a validated, schema-able contract.

`str` is the text sibling of `bytes`, and it does **not** mean raw JSON: returning
`'{"status": "ok"}'` sends that literal text as `text/plain`, not JSON. JSON stays
Struct-only.

## Controlling headers & status

When you need to set headers or override the status, return a wrapper. They're
generic so the body and header **types are preserved**, not erased:

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
            headers=WidgetHeaders(x_cache="hit", x_rate_limit=100),
        )


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

- `JSONResponse[T: Struct, H: Struct | None = None]` — `json: T`, encoded with the
  same fast msgspec path as a plain return (the wrapper itself is never serialized).
- `BytesResponse[H: Struct | None = None]` — `content: bytes`, octet-stream.

The body type is **required** (`JSONResponse[Widget]`) — that's the point: reaching
for a wrapper never costs you the schema. The header type `H` defaults to `None`, so
`JSONResponse[Widget]` is a body with no typed headers.

## Headers

Three ways to control what a response carries: typed headers, cookies, and the raw
escape hatch — the typed/raw split mirrors how a handler [receives](binding.md#headers-headers-typed-and-raw_headers-opaque) headers.

### Typed — `headers`

A `Struct`, for the conventional 99%. Field names map to wire names by the inverse of
the request mangle (`x_trace_id` → `x-trace-id`); values are encoded as strings —
scalars plainly (`bool` → `true`/`false`), nested Structs/lists as JSON. `None`-valued
optional fields are simply omitted.

```python
class Headers(Struct):
    x_request_id: str
    x_rate_remaining: int
    x_debug: DebugInfo | None = None   # a Struct -> JSON string; None -> omitted


JSONResponse(json=widget, headers=Headers(x_request_id="abc", x_rate_remaining=42))
# X-Request-Id: abc
# X-Rate-Remaining: 42
```

This is the typed path the OpenAPI spec will describe.

### Cookies — `cookies`

A `Sequence[SetCookie]`, one `Set-Cookie` header per entry, secure by default. This is
the blessed way to set a cookie — see [Cookies](cookies.md) for the full page
(`SetCookie`'s secure-by-default attributes, deleting with `SetCookie.expire`, and
cookie auth):

```python
from jero import SetCookie

JSONResponse(json=widget, cookies=[SetCookie("session_id", token)])
```

### Raw — `raw_headers`

The escape hatch for exotic names now: literal underscores or specific casing. A plain
mapping, or a `RawHeaders` (pass a request's straight through to forward it, repeats
and all) — still how you'd hand-roll a repeated header jero has no typed vocabulary
for:

```python
from jero import RawHeaders

JSONResponse(
    json=widget,
    raw_headers=RawHeaders([("X-Trace-Id", "a"), ("X-Trace-Id", "b")]),
)
```

Emission order when several are given: typed `headers` first, then one `Set-Cookie`
pair per `cookies` entry, then `raw_headers` last — so its own repeats still survive.
`content-type` defaults per kind and `content-length` is always managed by the
framework (ignored if you supply it).

> The rule of thumb: a typed `Struct` for the conventional case, `cookies` for
> `Set-Cookie` specifically, and `raw_headers` only for exact wire control on anything
> else non-conventional — casing, underscores, a repeat jero has no typed name for.

## Status codes

Every wrapper carries `status_code: int | None`. Leave it `None` to use the verb's
default (201 for `create`, else 200) — or, for the wrappers that fix a status of their own,
that status ([below](#dynamic-success-status)). Set it to override either:

```python
from msgspec import Struct

from jero import BaseApp, JSONResponse, Resource


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


class WidgetResource(Resource, path="/widgets"):
    async def create(self, json: WidgetIn) -> JSONResponse[Widget]:
        widget = Widget(id="widget-id", name=json.name)
        return JSONResponse(json=widget, status_code=202)   # Accepted


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

`status_code` is available on `BytesResponse` and the [streaming responses](streaming.md)
too.

## Naming a response type

`JSONResponse[Widget, CacheHeaders]` gets repetitive across a dozen handlers. Give it a name
with a `type` alias, or by subclassing the wrapper. Both resolve to the same document:

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
        return JSONResponse(json=Widget(id="w1", name="gizmo"), headers=CacheHeaders())


class Enveloped(Endpoint, path="/enveloped"):
    async def get(self) -> Envelope[Widget]:
        return JSONResponse(json=Widget(id="w1", name="gizmo"), headers=CacheHeaders())


class Subclassed(Endpoint, path="/subclassed"):
    async def get(self) -> Cached:
        return Cached(json=Widget(id="w1", name="gizmo"))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(Aliased())
        self._include_endpoint(Enveloped())
        self._include_endpoint(Subclassed())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

All three document one 200 whose schema refs `Widget`, with `cache-control` in `headers` —
the name you chose appears nowhere in the spec. Pick whichever reads better: an alias when
you only want the name, a subclass when it should also carry defaults. Aliases of aliases
resolve, either form works as a union member (`WidgetResponse | NoContent`), and a subclass
may stay generic like the alias (`class Envelope[T: Struct](JSONResponse[T, CacheHeaders])`,
written `-> Envelope[Widget]`).

One rule: a wrapper must *say* what its body is. `-> JSONResponse` with no `[Widget]`
anywhere in the chain fails at startup with `must name its body type` — checked when
`_include_openapi` is wired, like the other spec-shape checks below. `SSEResponse` is the
exception, its `T` defaulting to `str`.

## Dynamic success status

A handler can answer with **different** success statuses depending on what it finds,
while both branches stay statically typed and both show up in the OpenAPI spec: return a
**union of response wrappers**.

Three wrappers exist for the success statuses REST actually uses beyond 200:

- `NoContent[H: Struct | None = None]` — 204, no body. Still carries `headers`,
  `raw_headers`, `location`, and `links` like any response (a 204 may legitimately carry
  a `Location`); at 204 neither `content-type` nor `content-length` is sent, since the
  status forbids them.
- `Created[T: Struct, H: Struct | None = None]` — 201 + a JSON body, regardless of the
  verb's own default.
- `Accepted[T: Struct, H: Struct | None = None]` — 202 + a JSON body, regardless of the
  verb's own default.

Any return type from this page can be a union member, **plain returns included** — a bare
`Struct` needs no wrapper just to sit beside a `NoContent`:

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
        return Widget(id=path.widget_id, name="gizmo")     # 200 + Widget


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

Both branches are documented: the spec's `200` response describes `Widget`, and its `204`
has no body. Reach for a wrapper on a branch only when that branch needs one —
`JSONResponse[Widget, Headers] | NoContent` to add typed headers to the 200, say.

Note what the 204 branch is *not* for. A widget that doesn't exist is a `404`, which
`read_one` already derives from having a `path` source — returning 204 for it would document
two statuses meaning the same thing. A union of success wrappers is for outcomes the caller
asked for; failures stay [errors you raise](errors.md).

Each member's status is the one it would have on its own: a plain `Struct`,
`list[Struct]`, `str`, `bytes`, `JSONResponse`, or `BytesResponse` takes the verb's
default (201 for `create`, else 200); `NoContent` / `Created` / `Accepted` take their own.

## Union reference

The precise merge and rejection rules, for when a union gets more ambitious than the
common case above.

### Members that share a status

Members are free to land on the *same* status. OpenAPI keys one response per status, so
they merge into it — bodies as one `anyOf`, header maps unioned, and differing media types
side by side:

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
        return Archived(id="widget-id", archived_at="2026-01-01")


class MergedHeaders(Endpoint, path="/merged-headers"):
    async def get(
        self,
    ) -> JSONResponse[Widget, CacheHeaders] | JSONResponse[Archived, RateHeaders]:
        return JSONResponse(json=Widget(id="widget-id", name="gizmo"), headers=CacheHeaders())


class Negotiated(Endpoint, path="/negotiated"):
    async def get(self, headers: AcceptHeaders) -> bytes | JSONResponse[Widget]:
        if headers.accept == "application/octet-stream":
            return b"raw bytes"
        return JSONResponse(json=Widget(id="widget-id", name="gizmo"))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(MergedBodies())
        self._include_endpoint(MergedHeaders())
        self._include_endpoint(Negotiated())
        self._include_openapi(title="Widgets", version="1.0")


app = App()
```

`MergedBodies` documents exactly what `JSONResponse[Widget | Archived]` documents: one 200
whose schema is `anyOf: [Widget, Archived]`, with a `discriminator` because the members are
tagged. The two spellings agree, so pick whichever reads better.

`MergedHeaders` shows wrappers merging the same way, each carrying its own typed headers:
the 200 gets both `cache-control` and `x-rate-limit`. Nothing is lost by merging — OpenAPI
response headers are emitted without `required`, so a single header Struct was never
asserting presence either. What you gain over hand-merging into
`JSONResponse[Widget | Archived, BothHeaders]` is that the type checker now enforces the
*pairing*: a `Widget` can't be returned with `RateHeaders`.

`Negotiated` shows members that encode *differently*. `content` is keyed by media type, so
they sit side by side rather than merging — the OpenAPI shape for content negotiation, and
what the handler is actually doing. Its 200 documents both `application/octet-stream` and
`application/json`.

### What's rejected

- A **streaming** member. Its sender owns the response lifecycle (disconnect handling,
  mid-stream failure) and can't be chosen after the handler has already returned.
- Members at one status that **disagree on a header** — two `H` Structs describing the
  same wire name with different types. One status, one header map, no way to say both.
- Members at one status whose bodies **differ but can't compose** into an `anyOf` — a
  `list[Struct]` (an array) beside a Struct (an object), say. Members describing the *same*
  body are fine and simply dedupe: `BytesResponse[CacheHeaders] | BytesResponse[RateHeaders]`
  is one binary body with both header sets.
- `-> Widget | None`, with a pointed message: it's the natural typo for this feature.
  Return `NoContent`, not `None`, for a 204.

The header and body-merge checks are questions about the generated document, so they run
when `_include_openapi` is enabled — like the streaming item-type checks.

## Errors

Raise a typed `HTTPError` subclass from a handler to short-circuit with a Problem
Details response. See [Errors](errors.md) for static and parameterized errors, custom
error bodies, and exception handlers, and [REST semantics](rest.md) for the full
status-code map.
