<div align="center">

<img src="https://raw.githubusercontent.com/RogerThomas/jero/main/docs/assets/jero-logo-light.png" alt="jero" width="440">

<p>
  <a href="https://pypi.org/project/jero/"><img src="https://img.shields.io/pypi/v/jero" alt="PyPI"></a>
  <a href="https://github.com/RogerThomas/jero/actions/workflows/main.yml?query=branch%3Amain"><img src="https://github.com/RogerThomas/jero/actions/workflows/main.yml/badge.svg?branch=main" alt="Build status"></a>
  <a href="https://codecov.io/gh/RogerThomas/jero"><img src="https://img.shields.io/codecov/c/github/RogerThomas/jero/main" alt="codecov"></a>
  <a href="https://app.codspeed.io/RogerThomas/jero?utm_source=badge"><img src="https://img.shields.io/endpoint?url=https://codspeed.io/badge.json" alt="CodSpeed"/></a>
  <a href="https://pypi.org/project/jero/"><img src="https://img.shields.io/pypi/pyversions/jero" alt="Python versions"></a>
  <a href="https://github.com/RogerThomas/jero/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/jero" alt="License"></a>
</p>

**An opinionated, msgspec-first ASGI micro-framework for Python 3.13+.**

<br>
<em>Engineered for performance from the ground up</em>
<br>
<em>Strictly typed end to end</em>
<br>
<em>A joy to build on</em>
<br>
<a href="https://github.com/RogerThomas/jero/">GitHub</a> · <a href="https://RogerThomas.github.io/jero/">Documentation</a>

</div>

**jero** is an AI-powered ([a note on AI usage](docs/note-on-ai-usage.md)) framework
that builds typed JSON/REST APIs from plain classes. Annotate your handlers with
[msgspec](https://msgspec.dev/) Structs — jero does the rest: routing,
validation, serialization, auth, streaming, and resource lifecycle.

```python
from msgspec import Struct

from jero import BaseApp, Resource


class Widget(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:  # GET /widgets/{widget_id}
        return Widget(id=path.widget_id, name="gizmo")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

```bash
granian --interface asgi myapp:app    # or peregrine, uvicorn, or any ASGI server
```

No decorators, no `dict` returns, no runtime surprises — the `Struct` types *are* the
request/response contract, and they're verified at startup.

## Why jero?

<table>
<tr>
<td>⚡&nbsp;<strong>Fast</strong></td>
<td>The fastest Python ASGI framework across every workload in the <a href="#performance">benchmark</a>. All introspection happens once, at startup; the request path is just dict lookup → decode → call → encode.</td>
</tr>
<tr>
<td>🎯&nbsp;<strong>Opinionated</strong></td>
<td>One blessed way to do each thing, so you can't get it wrong. Contracts fail loud at startup with a precise <code>WiringError</code>, never quietly at runtime.</td>
</tr>
<tr>
<td>🔒&nbsp;<strong>Typed</strong></td>
<td>Fully static under pyright-strict, leaning hard into modern Python typing — PEP 695 generics (<code>JSONResponse[Body, Headers]</code>, <code>BaseApp[Factory]</code>), bounded type-params, generic inheritance, <code>Protocol</code>s. A handler's signature <em>is</em> its schema, and the source of the generated OpenAPI spec.</td>
</tr>
</table>

No DI container, either: dependencies are hand-wired in `wire`; the framework adds
only lifecycle — the one thing plain Python doesn't give you.

## What you get

- **[Resources & Endpoints](docs/guide/resources.md)** — REST CRUD by method name, or
  bare verbs for one-off routes.
- **[Bind by name, validated by msgspec](docs/guide/binding.md)** — `json`, `params`,
  `path`, `headers`, `form`, `user`; malformed → 400, schema-invalid → 422, all resolved
  once at startup.
- **[Typed responses *and* typed headers](docs/guide/responses.md)** —
  `JSONResponse[Body, Headers]` keeps both schemas (no erasure), `status_code` overrides
  the status, and `raw_headers` is the escape hatch for cookies and the exotic tail.
- **[Streaming, typed end to end](docs/guide/streaming.md)** — NDJSON, Server-Sent
  Events, and raw byte streams, with lifecycle teardown and client-disconnect handling
  done for you.
- **[Multipart forms & uploads](docs/guide/forms.md)** — typed parts, file uploads,
  per-part headers.
- **[Auth checked at startup](docs/guide/auth.md)** — the `user` type is verified
  against your authenticator before a single request is served, not at runtime.
- **[OpenAPI 3.1, derived](docs/guide/openapi.md)** — one `_include_openapi` call serves
  the spec and a Scalar UI, built from your types, docstrings, and `msgspec.Meta`
  constraints — no decorators.
- **[Lifecycle without a DI container](docs/guide/wiring.md)** — hand-wire in `wire`,
  open resources on exit stacks, group construction in a `BaseFactory`.
- **[REST semantics for free](docs/guide/rest.md)** — 404/400/422/401/405, auto `HEAD` +
  `OPTIONS`; opt any `Struct` into camelCase on the wire with `rename="camel"`.
- **[A real test story](docs/guide/testing.md)** — a sync, in-process `TestClient` (no
  network), streaming and typed WebSocket support, and a `factory=` seam for mocking.

Start with **[Getting Started](https://RogerThomas.github.io/jero/getting-started/)**, or
browse the full [Guide](https://RogerThomas.github.io/jero/).

## A real app

For anything real, a resource delegates to a service, and a `Factory` builds that
service — opening any resources it needs (HTTP clients, DB pools, …) on the app's
exit stacks, which jero closes in reverse at shutdown. The app is parameterised with
the factory type (`BaseApp[Factory]`), exposing it as `self._factory` in `wire`.

```python
from dataclasses import dataclass

from msgspec import Struct
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode
from pyreqwest.client import Client, ClientBuilder

from jero import BaseApp, BaseFactory, HTTPError, Resource


class WidgetNotFoundError(
    HTTPError,
    type="widget-not-found",
    title="Widget not found",
    status=404,
): ...


class WidgetPath(Struct):
    widget_id: str


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


@dataclass
class WidgetService:
    """Owns the upstream HTTP client; built once by the factory."""

    _client: Client

    async def fetch(self, widget_id: str) -> Widget:
        resp = await self._client.get(f"/widgets/{widget_id}").build().send()
        if resp.status == 404:
            raise WidgetNotFoundError()
        return json_decode(await resp.bytes(), type=Widget)

    async def create(self, data: WidgetIn) -> Widget:
        request = self._client.post("/widgets").header("Content-Type", "application/json")
        resp = await request.body_bytes(json_encode(data)).build().send()
        return json_decode(await resp.bytes(), type=Widget)


@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    # called as: POST /widgets
    async def create(self, json: WidgetIn) -> Widget:
        return await self._service.create(json)

    # called as: GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._service.fetch(path.widget_id)


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(
            ClientBuilder().base_url("https://api.example.com").build()
        )
        return WidgetService(client)


class App(BaseApp[Factory]):
    async def wire(self) -> None:
        widget_service = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widget_service))


app = App()
```

## Performance / Benchmarks

Four workloads, ten frameworks, one worker on one dedicated core each: Python (Django
Bolt, Blacksheep, Litestar, Robyn, FastAPI, Flask), Go (Gin), Bun (Elysia), and Java
(Spring Boot). **jero is the fastest Python framework on all four**, by 1.03× to 1.34×
over Django Bolt and 1.4× to 5.0× over Blacksheep, Litestar, and FastAPI. Per core, it
beats Gin on three of the four and is the fastest of all ten on the database workload.

<p align="center">
  <img src="https://raw.githubusercontent.com/RogerThomas/jero/main/docs/assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python framework across all four workloads" width="820">
</p>

On the pure framework path (a typed JSON `GET`):

| Framework            | `GET /info` req/s | vs jero   |
| :------------------- | :---------------- | :-------- |
| elysia *(Bun)*       | 156.8k            | 1.55×     |
| **jero**             | **101.2k**        | **1.00×** |
| django-bolt          | 90.5k             | 0.89×     |
| gin *(Go)*           | 89.3k             | 0.88×     |
| blacksheep           | 73.4k             | 0.73×     |
| litestar             | 49.6k             | 0.49×     |
| robyn                | 43.8k             | 0.43×     |
| spring-boot *(Java)* | 42.4k             | 0.42×     |
| fastapi              | 27.3k             | 0.27×     |
| flask                | 27.3k             | 0.27×     |

All four workloads, the setup, and the caveats are in the
**[Performance / Benchmarks docs](https://RogerThomas.github.io/jero/performance/)**;
every app and the harness are at
**[api-benchmarks](https://github.com/RogerThomas/api-benchmarks)**.

## Development

```bash
task install   # create the venv and install pre-commit hooks
task check     # lock check + ruff, pyrefly, deptry, pylint (via prek)
task test      # run the test suite with coverage
```

See [`AGENTS.md`](AGENTS.md) for the design philosophy and the contract, and
[`style-guide.md`](style-guide.md) for project conventions.
