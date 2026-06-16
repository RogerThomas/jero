<div align="center">

<img src="docs/assets/jero-logo.png" alt="jero" width="440">

<p>
  <a href="https://pypi.org/project/jero/"><img src="https://img.shields.io/pypi/v/jero" alt="PyPI"></a>
  <a href="https://github.com/RogerThomas/jero/actions/workflows/main.yml?query=branch%3Amain"><img src="https://github.com/RogerThomas/jero/actions/workflows/main.yml/badge.svg?branch=main" alt="Build status"></a>
  <a href="https://codecov.io/gh/RogerThomas/jero"><img src="https://codecov.io/gh/RogerThomas/jero/branch/main/graph/badge.svg" alt="codecov"></a>
  <a href="https://pypi.org/project/jero/"><img src="https://img.shields.io/pypi/pyversions/jero" alt="Python versions"></a>
  <a href="https://github.com/RogerThomas/jero/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/jero" alt="License"></a>
</p>

**An opinionated, msgspec-first ASGI micro-framework for Python 3.14.**

<em>Nearly Go-fast. Typed end to end. A joy to build on.</em>

<a href="https://github.com/RogerThomas/jero/">GitHub</a> · <a href="https://RogerThomas.github.io/jero/">Documentation</a>

</div>

## What is jero?

jero is a fast and modern Python web framework for building typed JSON/REST APIs on
ASGI. You write resources and endpoints as plain classes and annotate handler inputs
and outputs with [msgspec](https://jcristharif.com/msgspec/) Structs; jero handles
the rest — routing, request/response validation, serialization, auth, and resource
lifecycle — and runs under any ASGI server (granian, uvicorn, …).

It's opinionated on purpose, and makes one bet: that being aggressively prescriptive
— rather than flexible — is exactly what lets a framework be *both* extremely fast
*and* a joy to build on. Three pillars, all non-negotiable:

1. **Speed.** Introspection happens once, at startup. The request path is dict
   lookup → msgspec decode → call → encode, and nothing else is ever added to it.
2. **Opinionated DX.** One blessed way to do each thing, encoded so you can't get it
   wrong. Contracts fail loud at startup with a precise `WiringError`, never quietly
   at runtime.
3. **Strict typing.** Fully static under pyright-strict — the types *are* the
   contract, and the source of the coming OpenAPI spec. If you don't like typing,
   this isn't your framework.

And no DI container: dependencies are hand-wired in `_wire`; the framework adds only
lifecycle — the one thing plain Python doesn't give you.

## What you get

- **Resources & Endpoints** — REST CRUD by method name, or bare verbs for one-off routes.
- **Bind by name, validated by msgspec** — `json`, `params`, `path`, `headers`, `form`,
  `user`; malformed → 400, schema-invalid → 422, all resolved once at startup.
- **Typed responses *and* typed headers** — `JSONResponse[Body, Headers]` keeps both
  schemas (no erasure), `status_code` overrides the status, and `raw_headers` is the
  escape hatch for cookies and the exotic tail.
- **Streaming, typed end to end** — NDJSON, Server-Sent Events, and raw byte streams,
  with lifecycle teardown and client-disconnect handling done for you.
- **Multipart forms & uploads** — typed parts, file uploads, per-part headers.
- **Auth checked at startup** — the `user` type is verified against your authenticator
  before a single request is served, not at runtime.
- **Lifecycle without a DI container** — hand-wire in `_wire`, open resources on exit
  stacks, group construction in a `BaseFactory`.
- **REST semantics for free** — 404/400/422/401/405, auto `HEAD` + `OPTIONS`, camelCase
  on the wire.
- **A real test story** — a sync, in-process `TestClient` (no socket), streaming support,
  and a `factory=` seam for mocking.

Start with **[Getting Started](https://RogerThomas.github.io/jero/getting-started/)**, or
browse the full [Guide](https://RogerThomas.github.io/jero/).

## Example

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str
    name: str


class WidgetResource(Resource):
    # called as: GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return Widget(id=path.widget_id, name="widget-name")


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource(), path="/widgets")


app = App()
```

Run it under any ASGI server, e.g. [granian](https://github.com/emmett-framework/granian):

```bash
granian --interface asgi myapp:app
```

### With a service layer and a factory

For anything real, a resource delegates to a service, and a `Factory` builds that
service — opening any resources it needs (HTTP clients, DB pools, …) on the app's
exit stacks, which jero closes in reverse at shutdown. The app is parameterised with
the factory type (`BaseApp[Factory]`), exposing it as `self._factory` in `_wire`.

```python
from dataclasses import dataclass

import httpx
from msgspec import Struct
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode

from jero import BaseApp, BaseFactory, HTTPError, Resource


class WidgetPath(Struct):
    widget_id: str


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


@dataclass
class WidgetService:
    """Owns the upstream HTTP client; built once by the factory."""

    _client: httpx.AsyncClient

    async def fetch(self, widget_id: str) -> Widget:
        resp = await self._client.get(f"/widgets/{widget_id}")
        if resp.status_code == 404:
            raise HTTPError(404, "widget not found")
        return json_decode(resp.content, type=Widget)

    async def create(self, data: WidgetIn) -> Widget:
        resp = await self._client.post("/widgets", content=json_encode(data))
        return json_decode(resp.content, type=Widget)


@dataclass
class WidgetResource(Resource):
    _service: WidgetService

    # called as: POST /widgets
    async def create(self, json: WidgetIn) -> Widget:
        return await self._service.create(json)

    # called as: GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._service.fetch(path.widget_id)


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(httpx.AsyncClient(base_url="https://api.example.com"))
        return WidgetService(client)


class App(BaseApp[Factory]):
    async def _wire(self) -> None:
        widgets = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widgets), path="/widgets")


app = App()
```

## Performance

jero is fast — very fast. In fact, it co-leads the quickest Python ASGI frameworks
and lands within a few percent of a hand-written Go (Gin) service on the same box.

The numbers below are from the authed write path — `POST /movies` (bearer auth →
msgspec decode → handler → encode → `201`) — run natively under granian with a single
worker (Go pinned to `GOMAXPROCS=1`), driven by [oha](https://github.com/hatoo/oha)
at concurrency 200:

| Framework | Requests/sec | Relative to jero |
| --- | --: | --: |
| Go / Gin *(reference)* | ≈ 45,200 | 1.03× |
| **jero** | **≈ 44,000** | **1.00×** |
| Blacksheep | ≈ 43,000 | 0.98× |
| Litestar | ≈ 22,000 | 0.50× |
| Robyn | ≈ 15,000 | 0.34× |
| FastAPI | ≈ 7,300 | 0.17× |

A statistical tie with Blacksheep, ~2× Litestar, ~3× Robyn, and ~6× idiomatic
FastAPI — at ~97% of raw Go on the same machine (and ~91% on a plain `GET`). These
are localhost runs and partly client-bound, so treat them as indicative; the
benchmark harness lives in a separate repo.

## Development

```bash
task install   # create the venv and install pre-commit hooks
task check     # lock check + ruff, pyright, deptry, pylint (via prek)
task test      # run the test suite with coverage
```

See [`AGENTS.md`](AGENTS.md) for the design philosophy and the contract, and
[`style-guide.md`](style-guide.md) for project conventions.
