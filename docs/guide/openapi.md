# OpenAPI & docs

jero generates an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document from the
types you already wrote. One call in `wire` serves it:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint


class Widget(Struct):
    id: str


class WidgetsEndpoint(Endpoint, path="/widgets"):
    async def get(self) -> list[Widget]:
        return [Widget(id="widget-id")]


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(WidgetsEndpoint())
        self._include_openapi(title="Widgets API", version="1.0.0")


app = App()
```

- **`/openapi.json`** serves the document.
- **`/docs`** serves a [Scalar](https://github.com/scalar/scalar) docs UI
  ([options](#the-docs-ui)).
- `_include_openapi` can come before or after your routes: the document is built once,
  after wiring finishes.
- The spec routes themselves are never documented.

## What's derived

Everything in the document comes from the wiring you already did:

| OpenAPI                  | Derived from |
| ------------------------ | ------------ |
| `paths` + operations     | each wired `Resource`/`Endpoint` method and its mount path |
| `parameters`             | the `path` / `params` / `headers` / `cookies` source Structs, expanded field by field |
| `requestBody`            | the `json` body Struct (a tagged union becomes `anyOf` + `discriminator`), or `content` bytes, or a `form` |
| `responses` (success)    | the handler's return type: a `Struct`, `list[Struct]`, `str`, `bytes`, a `JSONResponse[T]` / `NoContent` / `Created[T]` / `Accepted[T]`, or a streaming response. A *union* of these yields one entry per status |
| `responses` (errors)     | the sources an operation actually has (see [Error responses](#error-responses)) |
| `components.schemas`     | every referenced `Struct`, via msgspec, with `rename` and `msgspec.Meta` honored |
| `security`               | the `auth` an operation is mounted behind |
| `summary` / `description`| `OperationMeta.summary` / `.description` (explicit; docstrings are never published) |
| model `description`      | a model's `ModelMeta` via `jero.Struct`'s `meta=` (explicit; not its docstring) |
| `operationId`            | the shape and method name, e.g. `WidgetResource_readOne` |
| `tags`                   | `meta` / `meta_<op>` (see [Tags](#tags)) |

> **Docstrings are never published.** A docstring is a note to maintainers, so it can't
> leak into the public spec. Public prose is always explicit: `OperationMeta.summary` /
> `description` for operations, `ModelMeta` for models, field `Meta` for fields.

### Error responses

jero returns errors as RFC 9457 Problem Details
(`{"type", "title", "status", "docs?"}`, plus `"detail"` and `"params"` on a
parameterized error; see [Errors](errors.md)). The spec lists only the errors an
operation can actually produce:

| Status | Documented when the operation… |
| ------ | ------------------------------ |
| `400`  | binds a body, query params, headers, or cookies (malformed request) |
| `401`  | is mounted behind `auth` |
| `404`  | binds a `path` |
| `415`  | takes a `form` (wrong media type) |
| `422`  | binds a body (well-formed but invalid) |
| `500`  | always (an unhandled error) |

So a bodyless, unauthenticated `GET` lists only its success response and `500`. All
derived error responses point at one shared `Problem` schema.

## Describing models

### Schemas and `msgspec.Meta`

Models are schema'd by msgspec's own
[`schema_components`](https://jodie.dev/msgspec/api.html#msgspec.json.schema_components).
Annotate a field with `msgspec.Meta` and it is both validated on the request and
documented:

```python
from typing import Annotated

from msgspec import Meta, Struct


class WidgetIn(Struct, rename="camel"):
    name: Annotated[str, Meta(min_length=1, description="Human-readable name")]
    price_cents: Annotated[int, Meta(ge=0, description="Price in cents")]
```

- `ge` / `le` become `minimum` / `maximum`.
- `min_length` / `max_length`, `pattern`, `description`, `examples`, and `title` all
  appear on the schema.
- `rename="camel"` makes the property `priceCents` on the wire.

**Whole-object examples.** When every field carries `examples`, jero zips them by index
into whole-object examples on the request/response media type, where docs UIs like
Scalar render them as a selectable sample:

```python
class WidgetIn(Struct, rename="camel"):
    name: Annotated[str, Meta(examples=["Gadget", "Gizmo"])]
    price_cents: Annotated[int, Meta(examples=[1999, 2999])]
```

```jsonc
"examples": {
  "example 1": { "value": { "name": "Gadget", "priceCents": 1999 } },
  "example 2": { "value": { "name": "Gizmo",  "priceCents": 2999 } }
}
```

- Nothing is composed unless every field has examples: a partial object would omit
  required fields.
- A field with fewer examples reuses its last one.
- A `list[...]` response example is the array of all composed objects.

### Defining models

jero exports its own `Struct`: a drop-in for `msgspec.Struct` (same fields, config
keywords, encode/decode, `isinstance` checks) plus an optional `meta=` class keyword for
OpenAPI metadata. The recommended setup is one project base that fixes your wire
convention, inherited everywhere:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import Struct, ModelMeta

class Base(Struct, rename="camel"):            # one project base
    pass

class Page(Base):                              # no meta: identical to a plain Struct
    limit: int = 20

class Widget(Base, meta=ModelMeta(description="A sellable widget.")):
    name: str
    meta: dict     # a wire field named `meta` is fine
```

- **`ModelMeta(description=...)`** lands on the model's component schema. It is the only
  source of a model description, the same way `Resource`/`Endpoint` take their `meta`.
- **The `meta=` keyword and a wire field named `meta`** are different namespaces, so
  they never collide.
- **A plain `msgspec.Struct` works everywhere** jero accepts a model. It gets no
  model-level `description`; field-level `msgspec.Meta` descriptions reach the schema
  either way.

### Component names

A model's key under `components.schemas`, and every `$ref` that points at it, is its
class name. `ModelMeta(name=...)` overrides it:

```python
class Widget(Base, meta=ModelMeta(name="PublicWidget")):
    name: str
```

Use it to give a model a stable public name independent of the Python class, or to tell
apart two same-named Structs. Two models resolving to the same component name is a
startup `WiringError`.

## Overriding and extending

The derived document is the baseline. Declare metadata on the class to refine it:

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
        return Widget(id=path.widget_id)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_openapi(title="Widgets API", version="1.0.0")


app = App()
```

- **`meta`** applies to every operation of the class; **`meta_<op>`** to one. See
  [Metadata](resources.md#metadata).
- **`summary` / `description`** give the operation its prose.
- **`operation_id`** replaces the derived `operationId`.
- **`responses`** adds a response jero can't infer (a domain `409`), or overrides a
  derived one by reusing its status.

Responses cascade by status: derived, then class-level `meta.responses`, then
per-operation `meta_<op>.responses`. The most specific wins.

### Documenting errors

A `ResponseSpec` is a hand-written entry. For errors you already have classes for,
declare the classes with `exceptions` and the whole entry is derived from the error:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, HTTPError, OperationMeta


class Widget(Struct):
    id: str


class WidgetGoneError(HTTPError, type="widget-gone", title="Widget gone", status=410): ...


class WidgetsEndpoint(
    Endpoint,
    path="/widgets",
    meta_get=OperationMeta(exceptions=[WidgetGoneError]),
):
    async def get(self) -> list[Widget]:
        return [Widget(id="widget-id")]


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(WidgetsEndpoint())
        self._include_openapi(title="Widgets API", version="1.0.0")


app = App()
```

What the entry takes from the class:

- **Status**: the class's `status` (410 here).
- **Description**: the `title` of an `HTTPError`, or the `description` of a
  [`StructHTTPError`](errors.md#bring-your-own-error-body).
- **Body schema**: the per-class problem shape, with `type` and `status` as consts, so
  the spec says which codes an operation emits. A parameterized error adds its `params`
  schema. A `StructHTTPError` documents its composed wire model, with its `consts` and
  status as enum consts.

With an [error body adapter](errors.md#house-wide-error-format) registered, the Problem
family documents the adapter's body instead, derived responses included.

`exceptions` also lives on `EndpointMeta` / `ResourceMeta`. The three response sources
combine per status:

| Source | Class level vs operation level | Precedence per status |
| :-- | :-- | :-- |
| explicit `responses` (`ResponseSpec`) | operation **overrides** class | highest: always the last word |
| declared `exceptions` (error classes) | class **extends** operation (both remain raiseable) | middle: **joins** a derived entry at its status |
| derived (from the operation's sources) | n/a | baseline |

- Several errors sharing a status merge into one entry with an `anyOf` of their bodies.
- That includes a derived one. Declare your own 422 and the entry documents both it and
  the framework's 422 for a malformed body, since the operation can still send either.
- An entry that isn't a concrete jero error class is a startup `WiringError`.

#### Keeping the list complete

`exceptions` is the one part of the spec you maintain by hand, and a new `raise` deep in
a service can leave it behind. [`TestClient`](testing.md) checks it for you. Once an
operation declares `exceptions` (at either level), any test request to it that answers
with an error the spec leaves out fails:

```text
AssertionError: GET /widgets returned WidgetLockedError (423), which its exceptions=[...] does not declare
```

- **The declaration is the switch.** An operation that declares no `exceptions` isn't
  checked, and wiring `_include_openapi` or not changes nothing.
- **Exact classes.** A subclass of a declared error documents its own `type`, so it
  needs its own entry.
- **Whatever the spec documents passes.** That is:
    - the framework's own errors derived from the operation's sources (a malformed
      body's 422, a missing credential's 401, the generic 500), even when you declare
      your own error at the same status;
    - the errors the operation's authenticator
      [declares once](auth.md#declaring-the-authenticators-errors);
    - any error at a status an explicit `ResponseSpec` takes over.
- **The error sent is the one checked.** When an exception handler translates an
  exception into an error class, that error class is checked, not the exception. A
  handler that answers with an `ExceptionResponse` isn't checked, since an
  `ExceptionResponse` can't be listed in `exceptions`.
- **Only exercised paths.** Only the error paths your tests exercise are checked.

### Tags

Tags are the groups an operation belongs to. A `tags` entry is either a bare `str` (the
tag name) or a `Tag`, which defines that name with a description inline. A class-level
`meta` tag is the baseline, and an operation's `meta_<op>` extends or replaces it:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import Tag

meta=ResourceMeta(tags=[Tag("widgets", "Create, read, and manage widgets.")]),  # baseline + describes it
meta_create=OperationMeta(tags=["admin"]),               # list  -> ["widgets", "admin"]
meta_delete=OperationMeta(tags=("danger",)),             # tuple -> ["danger"]  (replaces)
meta_read_one=OperationMeta(operation_id="getWidget"),   # no tags -> inherits ["widgets"]
```

| `meta_<op>.tags` | Result |
| :-- | :-- |
| not set | inherits the class tags |
| a `list` | **extends** them (union, de-duplicated, order preserved) |
| a non-empty `tuple` | **replaces** them |

#### Tag descriptions and order

A tag's description is the blurb docs UIs render under the section heading. It lives on
the document's tag list, because OpenAPI has no operation-level tag description. There
are two ways to put one there:

- **Inline**: a `Tag(name, description)` anywhere the tag is used (above) is hoisted to
  the document. Reference the same tag by bare name (`"widgets"`) elsewhere.
- **Centrally**: `_include_openapi(tags=[...])`, which also fixes the order sections
  appear in:

```python
self._include_openapi(
    title="Widgets API", version="1.0.0",
    tags=[
        Tag("widgets", "Create, read, and manage widgets."),
        Tag("system"),  # description optional: here just to pin the order
    ],
)
```

A tag may be used without ever being described: it is a bare section, which OpenAPI
allows. Describing the same name two different ways, anywhere, is a startup
`WiringError`.

## Security schemes

An operation mounted behind `auth` gets a `security` requirement, and the spec carries
the matching `securitySchemes` entry. Subclass an auth base to pick the scheme:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import BearerAuth


class TokenAuth(BearerAuth):    # -> {"type": "http", "scheme": "bearer"}
    async def authenticate(self, headers: Credentials) -> User:
        ...
```

`BearerAuth` and `BasicAuth` are shorthand for a class attribute,
`openapi_security: ClassVar[SecurityScheme]`, that any authenticator can set. For any
other shape, set it yourself:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from typing import ClassVar

from jero import SecurityScheme


class ApiKeyAuth:
    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.api_key(
        name="x-api-key", location="header"
    )

    async def authenticate(self, headers: ApiKeyHeaders) -> User:
        ...
```

- **Constructors.** `SecurityScheme` has three: `http_bearer()`, `http_basic()`, and
  `api_key(name=..., location="header" | "query" | "cookie")`.
- **Nothing declared.** With the attribute absent or `None`, jero derives the scheme
  from what `authenticate` binds. The
  [auth page](auth.md#auth-in-the-openapi-spec) has the table.
- **Anything else is a `WiringError`** at startup: the attribute must be a
  `SecurityScheme` or `None`.
- **Anonymous callers.** An operation whose authenticator
  [accepts anonymous callers](auth.md#optional-authentication) advertises its scheme
  and an empty requirement beside it: `"security": [{"bearerAuth": []}, {}]`.

## The docs UI

`/docs` serves a [Scalar](https://github.com/scalar/scalar) reference loaded from a CDN
and pointed at `/openapi.json`. Tune the serving with `_include_openapi`:

```python
self._include_openapi(
    title="Widgets API",
    version="1.0.0",
    description="Manage widgets.",     # info.description
    openapi_path="/openapi.json",      # where the spec is served
    docs_path="/docs",                 # set to None to omit the UI entirely
    servers=["https://api.example.com"],
    docs_html=None,                    # supply your own HTML for offline / strict-CSP hosting
    favicon=Path("assets/icon.png"),   # docs-page icon; or a URL string
)
```

At startup jero logs where the docs are served (at `INFO` on the `jero` logger):

```
[INFO] jero: Serving API docs at http://127.0.0.1:8000/docs
```

- With [`JERO_BASE_URL`](deployment.md#behind-a-reverse-proxy) naming the public origin,
  the line is a full, clickable URL. Otherwise it is the relative path, since jero
  doesn't know the server's bound host.
- With the UI disabled, it points at the spec instead.

The `demo_app/` package serves a live spec: wire it up, open `/docs`, and browse the
widgets API in Scalar.

### Favicon

Without `favicon`, the docs page has no icon and `/favicon.ico` 404s. It takes one of:

| `favicon=` | What happens |
| ---------- | ------------ |
| a `Path`   | read once at wiring and served precomputed at `/favicon.ico` |
| a `str` URL (`data:` URIs work) | emitted verbatim in the `<link>`; nothing is served |

- A missing or unreadable file, or a suffix other than `.ico` / `.png` / `.svg`, is a
  `WiringError`.
- `/favicon.ico` never appears in the generated document.
- A custom `docs_html` page is never modified: reference the favicon yourself there.
