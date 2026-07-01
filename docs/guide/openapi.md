# OpenAPI & docs

jero generates an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document from the
types you already wrote — no decorators, no duplicate schema definitions. One call in
`wire` serves the spec as JSON and a [Scalar](https://github.com/scalar/scalar) docs UI:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint


class Widget(Struct):
    id: str


class WidgetsEndpoint(Endpoint, path="/widgets"):
    async def get(self) -> list[Widget]:
        """List widgets."""
        return [Widget(id="widget-id")]


class App(BaseApp):
    async def wire(self) -> None:
        self.include_endpoint(WidgetsEndpoint())
        self.include_openapi(title="Widgets API", version="1.0.0")


app = App()
```

That serves the document at **`/openapi.json`** and the docs UI at **`/docs`**. Order
doesn't matter — `include_openapi` can come before or after your routes, because the
document is built once after wiring finishes.

## What's derived

Everything in the document comes from the wiring you already did:

| OpenAPI                  | Derived from |
| ------------------------ | ------------ |
| `paths` + operations     | each wired `Resource`/`Endpoint` method and its mount path |
| `parameters`             | the `path` / `params` / `headers` source Structs, expanded field by field |
| `requestBody`            | the `json` body Struct (or `content` bytes, or a `form`) |
| `responses` (success)    | the handler's return type — a `Struct`, `list[Struct]`, `bytes`, a `JSONResponse[T]`, or a streaming response |
| `responses` (errors)     | the sources an operation actually has (see below) |
| `components.schemas`     | every referenced `Struct`, via msgspec — `rename` and `msgspec.Meta` honored |
| `security`               | the `auth` an operation is mounted behind |
| `summary` / `description`| `OperationMeta.summary` / `.description` (explicit; docstrings are never published) |
| model `description`      | a model's `ModelMeta` via `jero.Struct`'s `meta=` (explicit; not its docstring) |
| `operationId`            | the shape and method name, e.g. `WidgetResource_readOne` |
| `tags`                   | `meta` / `meta_<op>` (see [Metadata](resources.md#metadata)) |

The spec routes (`/openapi.json`, `/docs`) are never themselves documented.

### Schemas and `msgspec.Meta`

Models are schema'd by msgspec's own
[`schema_components`](https://jodie.dev/msgspec/api.html#msgspec.json.schema_components),
so the wire convention and field constraints come through for free. Annotate a field
with `msgspec.Meta` and it is both validated on the request *and* documented:

```python
from typing import Annotated

from msgspec import Meta, Struct


class WidgetIn(Struct, rename="camel"):
    name: Annotated[str, Meta(min_length=1, description="Human-readable name")]
    price_cents: Annotated[int, Meta(ge=0, description="Price in cents")]
```

`ge`/`le` become `minimum`/`maximum`, `min_length`/`max_length`, `pattern`,
`description`, `examples`, and `title` all appear on the schema, and `rename="camel"`
means the property is `priceCents` on the wire.

When **every** field carries `examples`, jero composes whole-object examples and attaches
them to the request/response **media type** (where docs UIs like Scalar render them as a
selectable sample), zipped by index — annotate each field once and full sample bodies
appear for free:

```python
class WidgetIn(Struct, rename="camel"):
    name: Annotated[str, Meta(examples=["Gadget", "Gizmo"])]
    price_cents: Annotated[int, Meta(examples=[1999, 2999])]
```

A request/response using `WidgetIn` then gets, on its `application/json` media type:

```jsonc
"examples": {
  "example 1": { "value": { "name": "Gadget", "priceCents": 1999 } },
  "example 2": { "value": { "name": "Gizmo",  "priceCents": 2999 } }
}
```

(The example is composed only if every field has examples — a partial object would omit
required fields. A field with fewer examples reuses its last; a `list[...]` response
example is the array of all composed objects.)

### Defining models

jero exports its own `Struct` — a drop-in for `msgspec.Struct` (same fields, config
keywords, encode/decode, `isinstance` checks) with one addition: an optional `meta=` class
keyword for OpenAPI metadata. **Recommended:** import `Struct` from `jero` and give your
project one base that fixes your wire convention; inherit it everywhere:

```python
from jero import Struct, ModelMeta

class Base(Struct, rename="camel"):            # one project base
    pass

class Page(Base):                              # no meta — identical to a plain Struct
    limit: int = 20

class Widget(Base, meta=ModelMeta(description="A sellable widget.")):
    name: str
    meta: dict     # a wire field named `meta` is fine — the class keyword and a field differ
```

The `meta=` description lands on the model's component schema. The class keyword and a wire
field named `meta` are different namespaces, so they never collide.

> **If you never need a model description, just use `msgspec.Struct`.** `jero.Struct` is
> only sugar for the `meta=` keyword; a plain `msgspec.Struct` works everywhere jero
> accepts a model — it simply gets no model-level `description`. Field-level `msgspec.Meta`
> descriptions flow into the schema either way.

### Model descriptions

A model's schema `description` is **explicit**, never taken from the class docstring (so a
maintainer note can't leak into the public spec) — it comes only from a `ModelMeta` passed
through the `meta=` keyword above, exactly like `Resource`/`Endpoint` take their `meta`.

### Component names

By default a model's key under `components.schemas` (and every `$ref` that points at it) is
its class name. `ModelMeta(name=...)` overrides it:

```python
class Widget(Base, meta=ModelMeta(name="PublicWidget")):
    name: str
```

Use it to give a model a stable public name independent of the Python class, or to
disambiguate two same-named Structs that would otherwise collide. Two models resolving to
the same component name is a startup `WiringError`.

> **Docstrings are never published.** Public prose is always explicit:
> `OperationMeta.summary`/`description` for operations, `ModelMeta` for models, field
> `Meta` for fields. A docstring stays what it should be — a note to maintainers.

### Error responses

jero returns a uniform error envelope (`{"error": "..."}`) with consistent statuses, so
the generator documents the errors an operation can *actually* produce — no false
entries:

| Status | Documented when the operation… |
| ------ | ------------------------------ |
| `400`  | binds a body, query params, or headers (malformed request) |
| `401`  | is mounted behind `auth` |
| `404`  | binds a `path` |
| `415`  | takes a `form` (wrong media type) |
| `422`  | binds a body (well-formed but invalid) |
| `500`  | always (an unhandled error) |

A bodyless, unauthenticated `GET` therefore lists only its success response and `500` —
not a `422` it could never return. All error responses point at one shared `Error`
schema.

## Overriding and extending

The derived document is the baseline; declare metadata at class definition to refine it.
`summary` / `description` give the operation its prose, and `responses` adds responses the
framework can't infer (a domain `409`) or overrides a derived one by reusing its status.

```python
from msgspec import Struct

from jero import BaseApp, OperationMeta, Resource, ResourceMeta, ResponseSpec


class Widget(Struct):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(
    Resource,
    path="/widgets",
    meta=ResourceMeta(tags=["widgets"]),               # every operation
    meta_create=OperationMeta(                          # just create
        operation_id="createWidget",
        summary="Create a widget",
        responses=[ResponseSpec(409, "A widget with that name already exists")],
    ),
):
    async def create(self, json: Widget) -> Widget:
        return json

    async def read_one(self, path: WidgetPath) -> Widget:
        """Fetch one widget by id."""
        return Widget(id=path.widget_id)


class App(BaseApp):
    async def wire(self) -> None:
        self.include_resource(WidgetResource())
        self.include_openapi(title="Widgets API", version="1.0.0")


app = App()
```

Responses cascade by status: derived → class-level `meta.responses` → per-operation
`meta_<op>.responses`, with the most specific winning.

**Tags** are the groups an operation belongs to. A `meta` tag entry is either a bare
`str` (the tag name — this is the OpenAPI operation-tag shape) or a `Tag` to define that
name *with a description* inline. They cascade by *container type*: a class-level `meta`
tag is the baseline, and an operation's `meta_<op>` extends or replaces it:

```python
from jero import Tag

meta=ResourceMeta(tags=[Tag("widgets", "Create, read, and manage widgets.")]),  # baseline + describes it
meta_create=OperationMeta(tags=["admin"]),               # list  -> ["widgets", "admin"]
meta_delete=OperationMeta(tags=("danger",)),             # tuple -> ["danger"]  (replaces)
meta_read_one=OperationMeta(operation_id="getWidget"),   # no tags -> inherits ["widgets"]
```

A `list` extends the class tags (union, de-duplicated, order preserved); a non-empty
`tuple` replaces them; the default inherits.

### Tag descriptions and order

A tag's **description** (docs UIs render it as the blurb under the section heading) lives
on the document's tag list — OpenAPI has no operation-level tag description. You get one
there in two ways:

- **Define it inline** with a `Tag(name, description)` anywhere it's used (above), and it's
  hoisted to the document. Reference the same tag by bare name (`"widgets"`) elsewhere.
- **Declare it centrally** on `include_openapi(tags=[...])`, which also fixes the order
  sections appear in:

```python
self.include_openapi(
    title="Widgets API", version="1.0.0",
    tags=[
        Tag("widgets", "Create, read, and manage widgets."),
        Tag("system"),  # description optional — here just to pin the order
    ],
)
```

A tag may be used without ever being described (it's a bare section, which OpenAPI
allows). The one rule: describing the **same name two different ways** — anywhere — is a
startup `WiringError`, so a tag's meaning can't silently fork.

## Security schemes

An operation mounted behind `auth` gets a `security` requirement. To advertise the
*scheme*, subclass one of the auth bases — the spec then carries the matching
`securitySchemes` entry:

```python
from jero import BearerAuth


class TokenAuth(BearerAuth[Credentials, User]):    # -> {"type": "http", "scheme": "bearer"}
    async def authenticate(self, headers: Credentials) -> User:
        ...
```

[`BearerAuth`](auth.md) and `BasicAuth` are sugar over an optional
`openapi_security: ClassVar[SecurityScheme]` attribute that *any* authenticator can set.
An authed route whose `Auth` declares nothing defaults to HTTP bearer. For other shapes,
set the attribute directly with a `SecurityScheme` constructor:

```python
from typing import ClassVar

from jero import SecurityScheme


class CookieAuth:
    # a bearer token in a cookie is apiKey/cookie in OpenAPI — there is no "bearer cookie"
    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.api_key(
        name="session", location="cookie"
    )

    async def authenticate(self, headers: Session) -> User:
        ...
```

`SecurityScheme` has three constructors: `http_bearer()`, `http_basic()`, and
`api_key(name=..., location="header" | "query" | "cookie")`.

## The docs UI

`/docs` serves a [Scalar](https://github.com/scalar/scalar) reference loaded from a CDN
and pointed at `/openapi.json`. Tune the serving with `include_openapi`:

```python
self.include_openapi(
    title="Widgets API",
    version="1.0.0",
    description="Manage widgets.",     # info.description
    openapi_path="/openapi.json",      # where the spec is served
    docs_path="/docs",                 # set to None to omit the UI entirely
    servers=["https://api.example.com"],
    docs_html=None,                    # supply your own HTML for offline / strict-CSP hosting
)
```

At startup jero logs where the docs are served (at `INFO` on the `jero` logger):

```
[INFO] jero: Serving API docs at http://127.0.0.1:8000/docs
```

jero is the ASGI app, not the server, so it doesn't know the bound host/port — the line
is a full, clickable URL only when [`JERO_BASE_URL`](wiring.md) names the public origin;
otherwise it's the relative path (`/docs`), and your server prints its own `Listening
at …` line with the host. With the UI disabled (`docs_path=None`) it points at the spec
instead.

The `demo_app/` package serves a live spec — wire it up, open `/docs`, and browse the
widgets API in Scalar.
