# Getting started

jero targets **Python 3.14** and runs under any ASGI server.

## Install

```bash
uv add jero
```

You'll also want an ASGI server to run it. [granian](https://github.com/emmett-framework/granian)
is a good default:

```bash
uv add granian
```

## Your first app

A jero app is a `BaseApp` subclass that wires up *resources* (REST collections) and
*endpoints* (single routes). Handler inputs and outputs are
[msgspec](https://jcristharif.com/msgspec/) `Struct`s — the types *are* the
request/response contract.

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str
    name: str


class WidgetResource(Resource):
    # GET /widgets/{widget_id}
    async def read_one(self, path: WidgetPath) -> Widget:
        return Widget(id=path.widget_id, name="widget-name")


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource(), path="/widgets")


app = App()
```

Run it:

```bash
granian --interface asgi myapp:app
```

```bash
curl localhost:8000/widgets/abc
# {"id":"abc","name":"widget-name"}
```

That's the whole loop: a `Struct` for the URL slots (`path`), a `Struct` for the
response, and a method name (`read_one`) that maps to `GET`.

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
- **Dependencies are hand-wired** in `_wire` — no DI container. The framework adds the
  one thing plain Python doesn't: resource lifecycle. See [Wiring & lifecycle](guide/wiring.md).

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
- [Request binding](guide/binding.md) — every way to get data into a handler.
- [Responses & headers](guide/responses.md) — typed bodies, typed headers, status codes.
- [Streaming](guide/streaming.md) — NDJSON, Server-Sent Events, and raw byte streams.
- [Authentication](guide/auth.md) · [Forms & uploads](guide/forms.md) ·
  [Wiring & lifecycle](guide/wiring.md) · [Testing](guide/testing.md) ·
  [REST & error semantics](guide/rest.md).
