# Responses & headers

What a handler returns is part of its type signature, so the response schema is known
at startup (and to the coming OpenAPI spec). There are two levels: return a plain
value when you just want a body, or a response wrapper when you want to control
headers or status.

## Plain returns

| Return type    | Sent as                                    |
| -------------- | ------------------------------------------ |
| a `Struct`     | `application/json`                         |
| `list[Struct]` | `application/json` (a JSON array)          |
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


class App(BaseApp):
    async def wire(self) -> None:
        self.include_resource(WidgetResource())
        self.include_endpoint(ExportEndpoint())


app = App()
```

A JSON body is **always** a `Struct` (or a list of them) — never a raw `dict`. A
`dict`/blob return is a `WiringError` at startup. That's the rule that gives every
endpoint a validated, schema-able contract.

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
        self.include_resource(WidgetResource())


app = App()
```

- `JSONResponse[T: Struct, H: Struct | None = None]` — `json: T`, encoded with the
  same fast msgspec path as a plain return (the wrapper itself is never serialized).
- `BytesResponse[H: Struct | None = None]` — `content: bytes`, octet-stream.

The body type is **required** (`JSONResponse[Widget]`) — that's the point: reaching
for a wrapper never costs you the schema. The header type `H` defaults to `None`, so
`JSONResponse[Widget]` is a body with no typed headers.

## Headers

Two ways to set response headers, mirroring how a handler [receives](binding.md#headers-headers-typed-and-raw_headers-opaque) them.

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

### Raw — `raw_headers`

The escape hatch for exotic names: literal underscores, specific casing, or **repeats**
(e.g. multiple `Set-Cookie`). A plain mapping, or a `RawHeaders` (pass a request's
straight through to forward it, repeats and all):

```python
from jero import RawHeaders

JSONResponse(
    json=widget,
    raw_headers=RawHeaders([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]),
)
```

When both are given, the typed `headers` are emitted first, then `raw_headers` is
appended — so its repeats survive. `content-type` defaults per kind and
`content-length` is always managed by the framework (ignored if you supply it).

> The rule of thumb: a typed `Struct` for the conventional case; drop to `raw_headers`
> for exact wire control — casing, underscores, repeats, anything non-conventional.

## Status codes

Every wrapper carries `status_code: int | None`. Leave it `None` to use the verb's
default (201 for `create`, else 200); set it to override:

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
        self.include_resource(WidgetResource())


app = App()
```

`status_code` is available on `BytesResponse` and the [streaming responses](streaming.md)
too.

## Dynamic success status

A handler can answer with **different** success statuses depending on what it finds,
while both branches stay statically typed and both show up in the OpenAPI spec: return a
**union of response wrappers**.

Three wrappers exist for the success statuses REST actually uses beyond 200:

- `NoContent[H: Struct | None = None]` — 204, no body. Still carries `headers`,
  `raw_headers`, `location`, and `links` like any response (a 204 may legitimately carry
  a `Location`); `content-type`/`content-length` are never sent.
- `Created[T: Struct, H: Struct | None = None]` — 201 + a JSON body, regardless of the
  verb's own default.
- `Accepted[T: Struct, H: Struct | None = None]` — 202 + a JSON body, regardless of the
  verb's own default.

```python
from msgspec import Struct

from jero import BaseApp, JSONResponse, NoContent, Resource


class Widget(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> JSONResponse[Widget] | NoContent:
        if path.widget_id == "missing":
            return NoContent()                                            # 204, no body
        return JSONResponse(json=Widget(id=path.widget_id, name="gizmo"))  # 200 + Widget


class App(BaseApp):
    async def wire(self) -> None:
        self.include_resource(WidgetResource())


app = App()
```

Both branches are documented: the spec's `200` response describes `Widget`, and its `204`
has no body.

The framework checks this shape at startup, not just at the type-checker:

- Every member must be a recognized wrapper — `NoContent`, `Created`, `Accepted`,
  `JSONResponse`, or `BytesResponse`. A bare `Struct`/`bytes` return, or a streaming
  response, can't join a union — the former has no type to carry a status, and a
  streaming sender owns the response lifecycle and can't be chosen after the fact.
- Every member must resolve to a **distinct status** — `JSONResponse[A] | JSONResponse[B]`
  is ambiguous (both would document/send 200); use `JSONResponse[A | B]` instead.
- `-> JSONResponse[Widget] | None` is rejected with a pointed message, since it's the
  natural typo for this feature: return `NoContent`, not `None`, for a 204.

## Errors

Raise a typed `HTTPError` subclass from a handler to short-circuit with a Problem
Details response. See [REST & error semantics](rest.md) for static and parameterized
errors, custom exception handlers, and the full status-code map.
