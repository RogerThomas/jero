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

jero is an AI-powered ([a note on AI usage](note-on-ai-usage.md)), [msgspec](https://msgspec.dev/)-first [ASGI](https://asgi.readthedocs.io/en/latest/) framework where your type hints are the API contract.
Routing, binding, validation, serialization, auth checks, and the coming OpenAPI
generation all derive from statically declared types, while the request path stays
close to raw msgspec performance: route lookup → decode → call → encode.

There are no route decorators and no dependency-injection container. Routes are plain
classes (`Resource` for REST collections, `Endpoint` for one-off routes); the method
name *is* the HTTP operation, and dependencies are ordinary constructor arguments you
wire by hand.

Almost everything that flows in and out of jero is a
msgspec `Struct`: request and response bodies,
headers, path params, query params, and multipart forms alike. You don't pass or return
raw `dict`s; the `Struct` is what gives jero validation, serialization, precise startup
checks, future
schema generation, and (most importantly) maximum performance from msgspec's compiled
codecs (whose [benchmarks](https://msgspec.dev/benchmarks) are almost hard to believe).

## Core principles

jero is opinionated on purpose. It makes one bet: being aggressively prescriptive,
rather than flexible, is how a framework can be *both* extremely fast *and* a joy to
build on.

| Principle          | What it means |
| ------------------ | ------------- |
| **Speed**          | Introspection happens once, at startup. The per-request path stays minimal and predictable. |
| **Opinionated&nbsp;DX** | One blessed way to do each thing, encoded so you can't get it wrong. Contracts fail loud at startup with a precise `WiringError`, never quietly at runtime. |
| **Strict typing**  | Fully static under pyrefly, with the public interface checked by every major type checker. Types are the contract, the validation source, and the source of the coming OpenAPI spec. |

jero leans hard into modern Python typing: [PEP 695](https://peps.python.org/pep-0695) generics
(`JSONResponse[Body, Headers]`, `BaseApp[Factory]`,
`NDJSONStreamingResponse[Movie]`), bounded type parameters with defaults, generic
inheritance, and `Protocol`s, so a handler's signature *is* its schema. If you don't
like typing, this isn't your framework.

For the reasoning behind those choices, read [Philosophy](philosophy.md). For a
feature-by-feature contrast with other Python frameworks, read
[Comparison](comparison.md).

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
        self.include_resource(WidgetResource())


app = App()
```

No `@app.get(...)`, no runtime route discovery: the class declares the path, and the
method name declares the operation.

Run it under any ASGI server, e.g. [granian](https://github.com/emmett-framework/granian):

```bash
granian --interface asgi myapp:app
```

New here? Start with [Getting Started](getting-started.md).

## Highlights

- **Resources & Endpoints** — CRUD by method name, or bare verbs for one-off routes.
  [→](guide/resources.md)
- **Bind by name, validated by msgspec** — `json`, `params`, `path`, `headers`, `form`,
  `user`, plus raw `content` / `raw_headers`. [→](guide/binding.md)
- **Typed responses *and* typed headers** — `JSONResponse[Body, Headers]` keeps both
  schemas; `status_code` overrides the status; `raw_headers` is the escape hatch for
  cookies and exotic names. [→](guide/responses.md)
- **Streaming, typed end-to-end** — NDJSON, Server-Sent Events, and raw byte streams,
  with lifecycle teardown and disconnect handling handled for you. [→](guide/streaming.md)
- **Multipart forms & uploads** — typed parts, file uploads, per-part headers.
  [→](guide/forms.md)
- **Background tasks** — drop a typed `Struct` on an in-process queue; a worker dispatches
  it to the handler registered for its type, drained at shutdown. [→](guide/background-tasks.md)
- **Auth that's checked at startup** — the `user` type is verified against the
  authenticator before the app serves a request. [→](guide/auth.md)
- **Lifecycle without a DI container** — hand-wire in `wire`, open resources on exit
  stacks, group construction in a `BaseFactory`. [→](guide/wiring.md)
- **REST semantics for free** — 404/400/422/401/405, auto `HEAD` + `OPTIONS`, camelCase
  on the wire. [→](guide/rest.md)
- **In-process `TestClient`** — sync, no socket, full lifespan, streaming support.
  [→](guide/testing.md)
- **Benchmark-led performance claims** — benchmarked side by side against Python, Go,
  and Bun frameworks, with the full methodology shown. [→](performance.md)

## API reference

The full public surface — `BaseApp`, `BaseFactory`, `Resource`, `Endpoint`, the
response and streaming types, and the test helpers — is documented in the
[API reference](modules.md).
