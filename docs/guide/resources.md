# Resources & Endpoints

jero has exactly two route-defining shapes. Pick by whether the route is a REST
collection or a one-off.

## Resource — REST collections

A `Resource` is a class. Define any of the six CRUD methods; their **names** decide
the HTTP method and status:

| Method            | HTTP            | Default status | Path                |
| ----------------- | --------------- | -------------- | ------------------- |
| `create`          | POST            | 201            | the mount path      |
| `read_one`        | GET (item)      | 200            | mount + item id     |
| `read_many`       | GET (collection)| 200            | the mount path      |
| `update`          | PUT             | 200            | mount + item id     |
| `partial_update`  | PATCH           | 200            | mount + item id     |
| `delete`          | DELETE          | 200            | mount + item id     |

```python
from msgspec import Struct

from jero import Resource


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource):
    async def create(self, json: WidgetIn) -> Widget:           # POST /widgets
        ...

    async def read_many(self) -> list[Widget]:                  # GET  /widgets
        ...

    async def read_one(self, path: WidgetPath) -> Widget:       # GET  /widgets/{widget_id}
        ...

    async def delete(self, path: WidgetPath) -> Widget:         # DELETE /widgets/{widget_id}
        ...
```

`read_many` serves the mount path itself and **cannot** extend it with trailing
segments — items belong to `read_one`. The framework enforces this at startup.

## Endpoint — single routes

An `Endpoint` is a class with bare verb methods (`get` / `post` / `put` / `patch` /
`delete`). There are no CRUD semantics: the method name *is* the verb, every verb
returns 200, and the path is exact. A different path is a different `Endpoint`.

```python
from msgspec import Struct

from jero import BaseApp, Endpoint


class Health(Struct):
    status: str


class HealthEndpoint(Endpoint):
    async def get(self) -> Health:        # GET /healthz
        return Health(status="ok")


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(HealthEndpoint(), path="/healthz")


app = App()
```

Use endpoints for health checks, webhooks, and actions that aren't a resource.

## Path templates

The mount path is a template: static segments plus `{slot}` params in `snake_case`.
A handler binds the slots through a `path` Struct whose fields must cover **every**
slot:

```python
class CollectionPath(Struct):
    collection_id: str
    item_id: str


class ItemResource(Resource):
    # GET /collections/{collection_id}/items/{item_id}
    async def read_one(self, path: CollectionPath) -> Item: ...
```

Rules, all checked at startup with a precise `WiringError`:

- Every `{slot}` in the mount path must be a field on the `path` Struct.
- Path Struct fields **cannot have defaults** — a URL segment is always present.
- For item routes (`read_one`, `update`, …) any `path` field *beyond* the template
  slots **extends** the URL as a trailing segment (the item id). For `read_many` and
  endpoints the path is exact — extra fields are an error.

Path values that fail conversion to their field type return **404** — a malformed id
doesn't identify a resource.

## Registering them

Resources and endpoints are wired in `BaseApp._wire`:

```python
class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource(), path="/widgets")
        self._include_endpoint(HealthEndpoint(), path="/healthz")
```

Routing is pure dict lookup: static routes match exactly; templated routes are
bucketed by `(method, segment-count)` and matched on their static segments — no
regexes, no route-table scans, no ordering rules. All of it is resolved **once**, at
wiring time.

See [Wiring & lifecycle](wiring.md) for how resources get their dependencies.
