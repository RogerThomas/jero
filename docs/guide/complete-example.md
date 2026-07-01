# Complete example

This is a small but complete jero app shape: factory, lifecycle-managed service,
authentication, path binding, JSON binding, typed response headers, a resource, and
`_wire`.

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, BaseFactory, HTTPError, JSONResponse, Resource


class InvalidTokenError(
    HTTPError,
    type="invalid-token",
    title="Invalid token",
    status=401,
): ...


class WidgetNotFoundError(
    HTTPError,
    type="widget-not-found",
    title="Widget not found",
    status=404,
): ...


class Credentials(Struct):
    authorization: str


class User(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str
    owner_id: str


class WidgetHeaders(Struct, omit_defaults=True):
    x_trace_id: str | None = None


class TokenAuth:
    def authenticate(self, headers: Credentials) -> User:
        token = headers.authorization.removeprefix("Bearer ").strip()
        if token != "token":
            raise InvalidTokenError()
        return User(id="user-id", name="user-name")


@dataclass
class WidgetStore:
    _widgets: dict[str, Widget]

    async def get(self, widget_id: str) -> Widget:
        try:
            return self._widgets[widget_id]
        except KeyError:
            raise WidgetNotFoundError() from None

    async def list_for_user(self, user: User) -> list[Widget]:
        return [widget for widget in self._widgets.values() if widget.owner_id == user.id]

    async def create(self, user: User, widget: WidgetIn) -> Widget:
        widget_id = f"widget-{len(self._widgets) + 1}"
        created = Widget(id=widget_id, owner_id=user.id, name=widget.name)
        self._widgets[created.id] = created
        return created


@dataclass
class WidgetService:
    _store: WidgetStore

    async def get_widget(self, user: User, widget_id: str) -> Widget:
        widget = await self._store.get(widget_id)
        if widget.owner_id != user.id:
            raise WidgetNotFoundError()
        return widget

    async def list_widgets(self, user: User) -> list[Widget]:
        return await self._store.list_for_user(user)

    async def create_widget(self, user: User, widget: WidgetIn) -> Widget:
        return await self._store.create(user, widget)


@asynccontextmanager
async def open_widget_store() -> AsyncIterator[WidgetStore]:
    widgets = {
        "widget-1": Widget(id="widget-1", owner_id="user-id", name="first-widget"),
    }
    yield WidgetStore(widgets)


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        store = await self._aenter(open_widget_store())
        return WidgetService(store)


@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    async def create(
        self,
        json: WidgetIn,
        user: User,
    ) -> JSONResponse[Widget, WidgetHeaders]:
        widget = await self._service.create_widget(user, json)
        return JSONResponse(json=widget, headers=WidgetHeaders(x_trace_id="trace-id"))

    async def read_many(self, user: User) -> list[Widget]:
        return await self._service.list_widgets(user)

    async def read_one(self, path: WidgetPath, user: User) -> Widget:
        return await self._service.get_widget(user, path.widget_id)


class App(BaseApp[Factory]):
    async def _wire(self) -> None:
        auth = TokenAuth()
        widgets = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widgets), auth=auth)


app = App()
```

The important part is where the framework boundary sits. The app constructs normal
Python objects in `_wire`, enters anything with lifecycle through the factory, and then
includes route classes. Handlers only declare typed inputs by name: `json` for the
request body, `path` for URL slots, and `user` for the authentication result.

Run it with an ASGI server:

```bash
granian --interface asgi myapp:app
```

Then call it with a bearer token:

```bash
curl -H "Authorization: Bearer token" localhost:8000/widgets/widget-1
# {"name":"first-widget","id":"widget-1","ownerId":"user-id"}
```

## A project-structured version

The example above is a single file so the whole shape is visible at a glance. For the
same idea split into the layout a real app would use (`config`, `models`, `auth`,
`services/`, `operations/`, `factory`, and `app` modules), see the
[`demo_app/`](https://github.com/RogerThomas/jero/tree/main/demo_app) package in the
repository. It's the widget app fleshed out with authentication, background analytics,
reverse-routed links, health checks, and streaming (an OpenAI-backed NDJSON endpoint and
a Server-Sent Events feed).

`demo_app` is also the app the test suite runs against (see the
[testing approach](testing-approach.md)), so it is always kept working, and as a typed
consumer of the public API it is type-checked by every major type checker in CI.
