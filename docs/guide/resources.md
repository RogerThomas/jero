# Resources & Endpoints

jero has exactly two route-defining shapes. Pick by whether the route is a REST
collection or a one-off.

## Resource — REST collections

A `Resource` is a class. Define any of the six CRUD methods; their **names** decide
the HTTP method and status:

```text
WidgetResource(path="/widgets")

create          POST    /widgets
read_many       GET     /widgets
read_one        GET     /widgets/{widget_id}
update_full     PUT     /widgets/{widget_id}
update_partial  PATCH   /widgets/{widget_id}
delete          DELETE  /widgets/{widget_id}
```

| Method            | HTTP            | Default status | Path                |
| ----------------- | --------------- | -------------- | ------------------- |
| `create`          | POST            | 201            | the mount path      |
| `read_one`        | GET (item)      | 200            | mount + item id     |
| `read_many`       | GET (collection)| 200            | the mount path      |
| `update_full`     | PUT             | 200            | mount + item id     |
| `update_partial`  | PATCH           | 200            | mount + item id     |
| `delete`          | DELETE          | 200            | mount + item id     |

```python
from msgspec import Struct

from jero import BaseApp, Resource


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def create(self, json: WidgetIn) -> Widget:      # POST /widgets
        return Widget(id="widget-id", name=json.name)

    async def read_many(self) -> list[Widget]:             # GET  /widgets
        return [Widget(id="widget-id", name="gizmo")]

    async def read_one(self, path: WidgetPath) -> Widget:  # GET  /widgets/{widget_id}
        return Widget(id=path.widget_id, name="gizmo")

    async def delete(self, path: WidgetPath) -> Widget:    # DELETE /widgets/{widget_id}
        return Widget(id=path.widget_id, name="gizmo")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
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


class HealthEndpoint(Endpoint, path="/healthz"):
    async def get(self) -> Health:  # GET /healthz
        return Health(status="ok")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(HealthEndpoint())


app = App()
```

Use endpoints for health checks, webhooks, and legacy paths you must serve verbatim.

## Actions on a resource

An "action" (*converse* on a conversation, *submit* on an order) usually doesn't need
an `Endpoint` — and reaching for one duplicates the resource's path template in a
second class. Model the action's **result as a sub-resource** instead, and the action
becomes an ordinary `create`:

```python
from msgspec import Struct

from jero import BaseApp, Resource


class MessageIn(Struct):
    text: str


class Message(MessageIn):
    id: str


class ConversationPath(Struct):
    conversation_id: str


# "converse" is a create on the conversation's messages
class MessageResource(Resource, path="/conversations/{conversation_id}/messages"):
    async def create(self, path: ConversationPath, json: MessageIn) -> Message:
        return Message(id="message-id", text=json.text)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(MessageResource())


app = App()
```

The noun does more than satisfy REST taste: the result gets an id and a `GET` for
free later, the 201/`Location` semantics fit, and the OpenAPI groups it under the
parent resource. State-transition actions (*cancel*, *archive*) often fit even more
simply as an `update_partial` flipping a status field. Fall back to an `Endpoint`
only when no noun works — a webhook receiver, a health probe, a legacy path.

## Path templates

A route declares its path **on the class**, at definition time. jero reads it once at
wiring — no path passed at the call site — so the class is the **single source of
truth** for its path, which is exactly what URL reversal
([`Link` / `Location`](links-and-location.md)) and the [OpenAPI generator](openapi.md)
read off it.

The mount path is a template: static segments plus `{slot}` params in `snake_case`.
A handler binds the slots through a `path` Struct whose fields must cover **every**
slot:

```python
from msgspec import Struct

from jero import BaseApp, Resource


class Item(Struct):
    id: str


class CollectionPath(Struct):
    collection_id: str
    item_id: str


class ItemResource(Resource, path="/collections/{collection_id}/items"):
    # GET /collections/{collection_id}/items/{item_id}
    async def read_one(self, path: CollectionPath) -> Item:
        return Item(id=path.item_id)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(ItemResource())


app = App()
```

Rules, all checked at startup with a precise `WiringError`:

- Every `{slot}` in the mount path must be a field on the `path` Struct.
- Path Struct fields **cannot have defaults** — a URL segment is always present.
- For item routes (`read_one`, `update_full`, …) any `path` field *beyond* the template
  slots **extends** the URL as a trailing segment (the item id). For `read_many` and
  endpoints the path is exact — extra fields are an error.

Path values that fail conversion to their field type return **404** — a malformed id
doesn't identify a resource.

## Registering them

Resources and endpoints are wired in `BaseApp.wire`:

```python
class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_endpoint(HealthEndpoint())
```

Routing is pure dict lookup: static routes match exactly; templated routes are
bucketed by `(method, segment-count)` and matched on their static segments — no
regexes, no route-table scans, no ordering rules. All of it is resolved **once**, at
wiring time.

## Metadata

Alongside the path, a route can declare OpenAPI metadata at class definition. `meta`
applies to every operation; `meta_<operation>` to one (`meta_get`, `meta_create`, …):

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, EndpointMeta, OperationMeta


class Widget(Struct):
    id: str


class WidgetsEndpoint(
    Endpoint,
    path="/widgets",
    meta=EndpointMeta(tags=["widgets"]),                 # all operations
    meta_get=OperationMeta(operation_id="listWidgets"),  # this operation
):
    async def get(self) -> list[Widget]:
        return [Widget(id="widget-id")]


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(WidgetsEndpoint())


app = App()
```

The three types — `EndpointMeta`, `ResourceMeta`, `OperationMeta` — carry `tags`,
`operation_id`, `summary`, `description`, and `responses` (`operation_id` lives only on
`OperationMeta`, so it can't cascade to every operation). They don't affect routing;
they refine the [auto-generated OpenAPI spec](openapi.md), which derives the rest from
your types (docstrings are never published — public prose is always explicit).

See [Wiring & lifecycle](wiring.md) for how resources get their dependencies.
