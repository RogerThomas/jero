# Wiring & lifecycle

jero has **no DI container**. You build your classes by hand in `wire`, and a dependency
is a constructor argument. The one thing jero adds is resource *lifecycle*: opening
things at startup and closing them at shutdown.

```python
from dataclasses import dataclass

from msgspec import Struct
from msgspec.json import decode as json_decode
from pyreqwest.client import Client, ClientBuilder

from jero import BaseApp, BaseFactory, Resource


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str
    name: str


@dataclass
class WidgetService:
    _client: Client

    async def get_widget(self, widget_id: str) -> Widget:
        response = await self._client.get(f"/widgets/{widget_id}").build().send()
        return json_decode(await response.bytes(), type=Widget)


@dataclass
class WidgetResource(Resource, path="/widgets"):
    _widget_service: WidgetService               # a dependency is a constructor argument

    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._widget_service.get_widget(path.widget_id)


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(             # opened now, closed at shutdown
            ClientBuilder().base_url("https://api.example.com").build()
        )
        return WidgetService(client)


class App(BaseApp[Factory]):
    async def wire(self) -> None:                # runs once, at startup
        widget_service = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widget_service))


app = App()
```

What each piece does:

- **`wire`** runs once at startup. It builds services and registers routes.
- **`Factory`** groups construction. Each `create_*` method builds one service.
- **`self._aenter(...)`** opens a resource and registers it to be closed at shutdown.
- **`self._factory`** is the factory jero built for you, because the app is declared as
  `BaseApp[Factory]`.
- **`WidgetResource(widget_service)`** is the whole of dependency injection: you pass the
  service in.

## `wire`

Subclass `BaseApp` and override `wire`. It is linear `async` code with no `yield`.

- A resource's dependencies are constructor arguments you pass in.
- To share a service across resources, build it once and pass it to each.

> **One naming rule, everywhere.** A name's spelling tells you who may call it:
>
> - **public**: called from outside the class, or the hook the framework calls on you
>   (like `wire`).
> - **`_name`**: what *you* call from inside your own subclass (`_include_*`, `_enter`,
>   `_aenter`, `_factory`, `_create_background_tasks`).
> - **`__name`**: jero's own. You can neither call nor override it.

## Lifecycle: `_enter` / `_aenter`

Anything that must be opened and closed (an HTTP client, a DB pool) is entered on the
app's exit stacks.

| Call | Enters | Returns |
| --- | --- | --- |
| `await self._aenter(cm)` | an async context manager | the opened resource |
| `self._enter(cm)` | a sync context manager | the opened resource |

- The app owns a sync
  [`ExitStack`](https://docs.python.org/3/library/contextlib.html#contextlib.ExitStack) and an
  [`AsyncExitStack`](https://docs.python.org/3/library/contextlib.html#contextlib.AsyncExitStack).
- Everything entered is closed in reverse order at shutdown, even if `wire` fails
  partway.
- Both helpers exist on the app and on the factory, so `wire` can call them directly
  too.

> **This is your ASGI lifespan.** `wire` runs on `lifespan.startup` and the exit stacks
> close on `lifespan.shutdown`. There is no separate startup or shutdown hook: teardown
> code goes in a context manager you enter here.
>
> Your server must therefore send lifespan events. peregrine, granian and uvicorn all
> do. With lifespan disabled (e.g. `uvicorn --lifespan off`) `wire` never runs, no
> routes are registered, and every request gets a 404.

## Factories

For anything real, group construction in a `BaseFactory`, as the example above does.

- Parameterize the app with it: `BaseApp[Factory]`. jero builds the factory at startup
  and injects the exit stacks.
- Inside `wire` it is `self._factory`.
- Its `create_*` methods use the same `_enter` / `_aenter` helpers. Anything they open
  is closed when the app shuts down.
- The factory owns the I/O services (the things with lifecycle). Pure in-memory wiring
  (an auth token map, say) can live directly in `wire`.

### Standalone use

A factory isn't tied to an app. For scripts, cron jobs and notebooks, `Factory.open()`
is the entry point:

```python
#!yeet
# doc-example: fragment (illustrative snippet, not a runnable app)
from dataclasses import dataclass

from pyreqwest.client import Client, ClientBuilder

from jero import BaseFactory


@dataclass
class WidgetService:
    _client: Client


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(ClientBuilder().build())
        return WidgetService(client)


async def main() -> None:
    async with Factory.open() as factory:
        widget_service = await factory.create_widget_service()
        ...  # the nightly job, the notebook cell, the script body
    # everything opened via _enter / _aenter is closed here
```

Save it as `job.py`, run `chmod +x job.py`, and invoke it with `uv run yeet ./job.py`.

- `open()` creates the exit-stack pair and builds the factory on them exactly as an app
  does at startup.
- Everything is unwound on exit, even if the block raises.
- Same factory, same `create_*` methods, same teardown guarantees. No app, routes or
  server.
- In sync *test* code, use [`FactoryHarness`](testing.md#factoryharness), the sync
  bridge over `open()`.

### The test seam

`BaseApp` accepts a prebuilt factory via `factory=`. Tests inject a stand-in factory
there, so the real services are never constructed:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from unittest.mock import create_autospec

factory_mock = create_autospec(Factory, spec_set=True, instance=True)
app = App(factory=factory_mock)
```

See [Testing](testing.md#mocking-dependencies) for the full pattern, and
[`FactoryHarness`](testing.md#factoryharness) for exercising a real factory's wiring in
isolation.

## Per-request resources

Lifecycle bound to a single request is an `async with` inside the handler. No framework
machinery is involved:

```python
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from msgspec import Struct

from jero import BaseApp, Resource


class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


@asynccontextmanager
async def open_txn() -> AsyncGenerator[None]:
    # acquire a per-request resource (a DB transaction, say); released on exit
    yield


class WidgetResource(Resource, path="/widgets"):
    async def create(self, json: WidgetIn) -> Widget:
        async with open_txn():
            return Widget(id="widget-id", name=json.name)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

## Why no resolver

Past lifecycle there is nothing to "resolve": a dependency is a constructor argument,
and `wire` is where you pass it. An injection or resolver system would add indirection,
not capability. Don't reach for one.
