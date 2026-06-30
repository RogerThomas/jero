# Request binding

Handler arguments bind **by name**. You declare only the ones you need; jero resolves
each from the request and validates it against your `Struct` — once at startup it
learns *which* sources a handler wants, and the request path just fills them in.

| Argument      | Source                          | Type                     |
| ------------- | ------------------------------- | ------------------------ |
| `json`        | request body (JSON)             | a `Struct`               |
| `content`     | request body (raw)              | `bytes`                  |
| `form`        | `multipart/form-data` body      | a `Struct` (see [Forms](forms.md)) |
| `params`      | query string                    | a `Struct`               |
| `path`        | URL template slots              | a `Struct`               |
| `headers`     | request headers                 | a `Struct`               |
| `raw_headers` | request headers (opaque)        | `RawHeaders`             |
| `user`        | the auth result                 | a `Struct` (see [Auth](auth.md)) |

`json`, `content`, and `form` are mutually exclusive (one request body), and are
rejected on bodyless verbs (`GET`, `DELETE`). Everything else can combine freely.

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
    async def update(self, path: WidgetPath, params: Page, json: WidgetIn) -> Widget:
        return Widget(id=path.widget_id, name=json.name)


class App(BaseApp):
    async def wire(self) -> None:
        self.include_resource(WidgetResource())


app = App()
```

## JSON body — `json`

The body is decoded straight into your `Struct` by msgspec. A malformed body → **400**;
a well-formed body that fails the schema → **422**.

A JSON body is **always** a `Struct`, never a raw `dict`. That's what gives it both
validation and a schema for the coming OpenAPI spec.

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
        self.include_resource(UploadResource())


app = App()
```

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
        self.include_endpoint(TraceEndpoint())


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


class HeadersEndpoint(Endpoint, path="/echo"):
    async def get(self, raw_headers: RawHeaders) -> Echo:    # GET /echo
        trace_id = raw_headers["X-Trace-Id"]         # case-insensitive lookup
        cookies = raw_headers.getlist("Cookie")      # repeats preserved
        return Echo(trace_id=trace_id, cookie_count=len(cookies))


class App(BaseApp):
    async def wire(self) -> None:
        self.include_endpoint(HeadersEndpoint())


app = App()
```

Use the typed `headers` Struct for values you act on; reach for `raw_headers` only for
forwarding the whole bag upstream or for diagnostics. The same split applies on the
[response side](responses.md#headers).

## camelCase (and any wire convention)

msgspec's `rename` is honored everywhere. Define a base Struct for your wire
convention and inherit it — snake_case in code, camelCase on the wire:

```python
class Camel(Struct, rename="camel"):
    ...


class WidgetIn(Camel):
    price_cents: int           # decoded from {"priceCents": ...}
```
