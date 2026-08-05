<p align="center">
  <a href=".">
    <img src="assets/jero-logo-light.png#only-light" alt="jero" width="440">
    <img src="assets/jero-logo-dark.png#only-dark" alt="jero" width="440">
  </a>
</p>

<p align="center"><strong>A msgspec-first ASGI micro-framework for Python 3.13+.</strong></p>

---

```bash
uv add jero
```

## What is jero?

jero is a [msgspec](https://msgspec.dev/)-first [ASGI](https://asgi.readthedocs.io/en/latest/)
framework where your type hints are the API contract ([a note on AI usage](note-on-ai-usage.md)).
Routing, binding, validation, serialization, auth checks, and [OpenAPI
generation](guide/openapi.md) all derive from statically declared types — introspected
once, at startup — so the request path stays minimal: dict lookup → msgspec decode →
handler call → encode.

There are no route decorators and no dependency-injection container. Routes are plain
classes (`Resource` for REST collections, `Endpoint` for one-off routes), the method
name *is* the HTTP operation, and dependencies are ordinary constructor arguments.
Everything that crosses the wire — bodies, headers, path and query params, forms — is a
msgspec `Struct`: one contract driving validation, serialization, startup checks,
schema generation, and msgspec's compiled-codec performance.

## Quickstart

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str
    name: str


class WidgetResource(Resource, path="/widgets"):
    # called as: GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return Widget(id=path.widget_id, name="widget-name")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

No `@app.get(...)`, no runtime route discovery: the class declares the path, and the
method name declares the operation.

Run it under any ASGI server, e.g. [granian](https://github.com/emmett-framework/granian):

```bash
granian --interface asgi myapp:app
```

In our four-scenario benchmark against seven frameworks — Python, Go, and Bun — jero is
the **fastest Python framework in every one**, methodology included. [→ Performance](performance.md)

New here? Start with [Getting Started](getting-started.md).

## Core principles

jero makes one bet: being aggressively prescriptive, rather than flexible, is how a
framework can be *both* extremely fast *and* a joy to build on.

| Principle          | What it means |
| ------------------ | ------------- |
| **Speed**          | Introspection happens once, at startup. The request path stays minimal and predictable. |
| **Opinionated&nbsp;DX** | One way to do each thing. Contracts fail loud at startup with a precise `WiringError`, never quietly at runtime. |
| **Strict typing**  | Types are the contract, the validation source, and the [OpenAPI](guide/openapi.md) source — and the public interface is checked by every major type checker. |

jero leans hard into modern Python typing: [PEP 695](https://peps.python.org/pep-0695)
generics (`JSONResponse[Body, Headers]`, `NDJSONStreamingResponse[Movie]`), bounded
type parameters with defaults, and `Protocol`s, so a handler's signature *is* its
schema. If you don't like typing, this isn't your framework.

For the reasoning behind those choices, read [Philosophy](philosophy.md). For a
feature-by-feature contrast with other Python frameworks, read
[Comparison](comparison.md).

## Highlights

- **Startup validation** — invalid apps can't boot: every contract is checked at wiring
  with a precise `WiringError`. [→](philosophy.md#startup-validation)
- **Typed responses *and* headers** — `JSONResponse[Body, Headers]` keeps both schemas;
  unions of responses document every status. [→](guide/responses.md)
- **Typed streaming** — NDJSON, SSE, and raw bytes, with lifecycle teardown and
  disconnect handling done for you. [→](guide/streaming.md)
- **Auth checked at startup** — the `user` type is verified against the authenticator
  before a single request is served. [→](guide/auth.md)
- **Reverse routing** — `Location` / `Link` headers built from the route class,
  validated at construction. [→](guide/links-and-location.md)
- **Compiled middleware & CORS** — fixed per-tier costs, zero on uncovered routes.
  [→](guide/middleware.md)
- **OpenAPI from your types** — a 3.1 spec plus Scalar docs UI, no duplicate schema
  definitions. [→](guide/openapi.md)
- **In-process `TestClient`** — sync, no socket, full lifespan, streaming support.
  [→](guide/testing.md)

## API reference

The full public surface — `BaseApp`, `BaseFactory`, `Resource`, `Endpoint`, the
response and streaming types, and the test helpers — is documented in the
[API reference](modules.md).
