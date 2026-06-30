# Getting started

jero targets **Python 3.13+** and runs under any ASGI server.

## Install

```bash
uv add jero
```

You'll also want an ASGI server to run it. [granian](https://github.com/emmett-framework/granian)
is a good default:

```bash
uv add granian
```

## Python version

jero requires **Python 3.13 or newer**. Python 3.12 and earlier are not supported.

The reason is generics. jero's response wrappers (and several other types) declare
type-parameter *defaults*, like the optional typed-headers parameter `H`:

```python
class JSONResponse[T: Struct, H: Struct | None = None]: ...
```

The `= None` default on a type parameter is [PEP 696](https://peps.python.org/pep-0696/),
which shipped in Python 3.13. The generic syntax itself
([PEP 695](https://peps.python.org/pep-0695/)) arrived in 3.12, so 3.12 can parse
`[T: Struct]` but not the `[H: Struct | None = None]` default. jero relies on those
defaults throughout, so 3.13 is the current floor, and there are no current plans to
lower it. If you have a need to run on an earlier version, please get in touch on
[GitHub Discussions](https://github.com/RogerThomas/jero/discussions).

## Your first app

A jero app is a `BaseApp` subclass that wires up *resources* (REST collections) and
*endpoints* (single routes). Handler inputs and outputs are
[msgspec](https://msgspec.dev/) `Struct`s — the types *are* the
request/response contract.

jero has no route decorators. Instead of writing `@app.get(...)`, you define a class,
declare its path on the class, and let method names carry the HTTP semantics.

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str
    name: str


class WidgetResource(Resource, path="/widgets"):
    # GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return Widget(id=path.widget_id, name="widget-name")


class App(BaseApp):
    async def wire(self) -> None:
        self.include_resource(WidgetResource())


app = App()
```

Run it:

```bash
granian --interface asgi myapp:app
```

```bash
curl localhost:8000/widgets/abc # -> { "id": "abc", "name": "widget-name" }
```

That's the whole loop: a `Struct` for the URL slots (`path`), a `Struct` for the
response, and a method name (`read_one`) that maps to `GET`.

The `Struct` requirement is deliberate. JSON request bodies, JSON responses, query
params, path params, headers, forms, auth users, and response headers all use typed
contracts. That is what gives jero validation, fast msgspec serialization, startup
errors for invalid wiring, and the source material for the [OpenAPI generator](guide/openapi.md).
If a handler returns a raw `dict`, jero can't prove or document its shape, so it is a
startup error.

## The mental model

- A **`Resource`** is a class with any of the CRUD methods `create` / `read_one` /
  `read_many` / `update` / `partial_update` / `delete`, mapped to POST / GET (item) /
  GET (collection) / PUT / PATCH / DELETE. See [Resources & Endpoints](guide/resources.md).
- An **`Endpoint`** is a class with bare verb methods (`get`/`post`/…) for non-resource
  routes — health checks, webhooks, actions.
- **Handler arguments bind by name**, each a `Struct`: `json`, `params`, `path`,
  `headers`, `form`, `user`, plus raw `content: bytes` / `raw_headers`. See
  [Request binding](guide/binding.md).
- **Returns are typed**: a `Struct`, `list[Struct]`, `bytes`, or a response wrapper
  (`JSONResponse[T]`, `BytesResponse`, a streaming response) when you need to control
  headers or status. See [Responses & headers](guide/responses.md).
- **Dependencies are hand-wired** in `wire` — no DI container. The framework adds the
  one thing plain Python doesn't: resource lifecycle. See [Wiring & lifecycle](guide/wiring.md).
- For a complete application shape, see the [complete example](guide/complete-example.md).

## Test it without a server

jero ships a synchronous, in-process [`TestClient`](guide/testing.md) — no socket, no
running server:

```python
from jero import TestClient


def test_read_one():
    with TestClient(App()) as client:
        resp = client.get("/widgets/abc")
        assert resp.status_code == 200
        assert resp.json() == {"id": "abc", "name": "widget-name"}
```

## Where next

- [Resources & Endpoints](guide/resources.md) — the routing model and path templates.
- [Complete example](guide/complete-example.md) — factory, service, auth, lifecycle,
  resource methods, typed binding, and typed responses together.
- [Request binding](guide/binding.md) — every way to get data into a handler.
- [Responses & headers](guide/responses.md) — typed bodies, typed headers, status codes.
- [Streaming](guide/streaming.md) — NDJSON, Server-Sent Events, and raw byte streams.
- [Authentication](guide/auth.md) · [Forms & uploads](guide/forms.md) ·
  [Wiring & lifecycle](guide/wiring.md) · [Testing](guide/testing.md) ·
  [REST & error semantics](guide/rest.md).
