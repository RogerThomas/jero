# Resources & Endpoints

jero has exactly two route-defining shapes. Pick by whether the route is a REST
collection or a one-off.

## Resource — REST collections

A `Resource` is a class. Define any of the six CRUD methods; their **names** decide
the HTTP method and status. For `path="/widgets"`:

| Method           | HTTP   | Path                   | Status |
| ---------------- | ------ | ---------------------- | ------ |
| `create`         | POST   | `/widgets`             | 201    |
| `read_many`      | GET    | `/widgets`             | 200    |
| `read_one`       | GET    | `/widgets/{widget_id}` | 200    |
| `update_full`    | PUT    | `/widgets/{widget_id}` | 200    |
| `update_partial` | PATCH  | `/widgets/{widget_id}` | 200    |
| `delete`         | DELETE | `/widgets/{widget_id}` | 200    |

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

The item id is the one `path` field the mount path doesn't name, appended as the last
segment. Its name is yours to choose, but it becomes the `{slot}` in the URL and the
OpenAPI spec, so name it for the resource (`widget_id`, not `id`). Item routes need
exactly one; `create` and `read_many` take none. Both are checked at startup.

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

The result gets an id, a later `GET`, 201/`Location` semantics, and an OpenAPI home
under its parent. State changes (*cancel*, *archive*) are often an `update_partial` on a
status field. Use an `Endpoint` only when no noun works.

When no noun works, the `Endpoint`'s path may end in a **custom method**: the last
segment's slot followed by `:verb`, the shape Google's AIP-136 uses
(`POST /widgets/{widget_id}:duplicate`). It sits beside the resource's own
`/widgets/{widget_id}` routes without clashing:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, Resource


class Widget(Struct):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:  # GET /widgets/{widget_id}
        return Widget(id=path.widget_id)


class WidgetDuplicateEndpoint(Endpoint, path="/widgets/{widget_id}:duplicate"):
    async def post(self, path: WidgetPath) -> Widget:  # POST /widgets/{widget_id}:duplicate
        return Widget(id=f"{path.widget_id}-copy")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_endpoint(WidgetDuplicateEndpoint())


app = App()
```

The slot binds everything before the suffix, so `POST /widgets/a:b:duplicate` binds
`widget_id="a:b"`. An action route is tried before a plain `{widget_id}` route on the same
verb, and a request whose suffix matches no action (or would leave the slot empty) falls
through to that plain route. Matching sees the path as the server decoded it, so
`x%3Aduplicate` reaches the action too. Reverse routing (`Location.from_operation(...)`)
and the OpenAPI path both carry the suffix. Because the action answers first, reversing the
plain route with an id that ends in the action's verb (`widget_id="x:duplicate"`) raises
rather than build a URL the action would answer. Keep ids free of `:`, as AIP-122 asks. A
wholly literal last segment such as `/widgets:batchGet` needs none of this: it is matched
as written.

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
- Item routes have exactly one field beyond the slots (the id); every other route has
  none.
- When two templates could match one path, the one with more literal segments wins
  (`/groups/special/items/{item_id}` over `/groups/{group_id}/items/{item_id}`), whatever
  order they were included in.
- A `:verb` action suffix goes only on the last segment of an `Endpoint` path, right
  after its slot. The verb is letters, digits, `-`, `.`, `_` or `~`.

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

All three types — `EndpointMeta`, `ResourceMeta`, `OperationMeta` — carry `tags`,
`responses`, and `exceptions`; `operation_id`, `summary`, and `description` live only on
`OperationMeta`, so they can't cascade to every operation. They don't affect routing;
they refine the [auto-generated OpenAPI spec](openapi.md), which derives the rest from
your types (docstrings are never published — public prose is always explicit).

See [Wiring & lifecycle](wiring.md) for how resources get their dependencies.
