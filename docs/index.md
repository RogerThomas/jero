<p align="center">
  <img src="assets/jero-logo.png" alt="jero" width="440">
</p>

<p align="center"><strong>A fast and modern, msgspec-first ASGI micro-framework for Python 3.14.</strong></p>

---

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

## Quickstart

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

## API reference

The full public surface — `BaseApp`, `BaseFactory`, `Resource`, `Endpoint`, the
response and streaming types, and the test helpers — is documented in the
[API reference](modules.md).
