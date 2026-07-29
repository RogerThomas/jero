# Wiring & lifecycle

jero has **no DI container** — and that's deliberate, not a gap. You hand-wire classes
in `wire`; a dependency is just a constructor argument. The one thing plain Python
doesn't give you for free — resource *lifecycle* — is the only thing the framework
adds.

## `wire`

Subclass `BaseApp` and override `wire`. It runs once at startup; here you construct
services and register routes. It's linear `async` code — no `yield`, no magic:

```python
class App(BaseApp):
    async def wire(self) -> None:
        service = WidgetService(...)
        self._include_resource(WidgetResource(service))
```

A resource's dependencies are constructor arguments you pass in. Want to share a
service across resources? Build it once and pass it to each.

> **One naming rule, everywhere.** A name's spelling tells you who may call it:
> **public** is called from outside the class (or is the hook the framework calls on
> you, like `wire`); **`_name`** is what *you* call from inside your own subclass
> (`_include_*`, `_enter`, `_aenter`, `_factory`, `_create_background_tasks`);
> **`__name`** is jero's own — you can neither call nor override it.

## Lifecycle: `_enter` / `_aenter`

Resources that must be opened and closed — HTTP clients, DB pools — are entered on the
app's exit stacks. The app owns a sync
[`ExitStack`](https://docs.python.org/3/library/contextlib.html#contextlib.ExitStack) and an
[`AsyncExitStack`](https://docs.python.org/3/library/contextlib.html#contextlib.AsyncExitStack)
and closes everything in reverse order at shutdown, even if `wire` fails partway:

```python
class App(BaseApp):
    async def wire(self) -> None:
        client = await self._aenter(niquests.AsyncSession())  # closed at shutdown
        cache = self._enter(open_cache())                     # sync context manager
        self._include_resource(WidgetResource(client, cache))
```

`_aenter(cm)` enters an async context manager; `_enter(cm)` a sync one. Both return the
opened resource and register it for teardown.

## Factories

For anything real, group construction in a `BaseFactory`. Parameterize the app with it
— `BaseApp[Factory]` — and jero builds the factory at startup, injecting the exit
stacks. It's then `self._factory` inside `wire`:

```python
from jero import BaseApp, BaseFactory


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(niquests.AsyncSession(base_url="https://api.example.com"))
        return WidgetService(client)


class App(BaseApp[Factory]):
    async def wire(self) -> None:
        widget_service = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widget_service))
```

The factory's `create_*` methods use the same `_enter` / `_aenter` helpers — anything
they open is closed when the app shuts down. The split is a useful seam: the factory
owns the I/O services (the things with lifecycle), while pure in-memory wiring (an auth
token map, say) can live directly in `wire`.

### Standalone use

A factory isn't tied to an app. For scripts, cron jobs, and notebooks,
`Factory.open()` is the blessed entry point: it creates the exit-stack pair, builds
the factory on them exactly as an app does at startup, and unwinds everything on
exit — even if the block raises:

```python
import asyncio
from dataclasses import dataclass

import niquests

from jero import BaseFactory


@dataclass
class WidgetService:
    _client: niquests.AsyncSession


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(niquests.AsyncSession())
        return WidgetService(client)


async def main() -> None:
    async with Factory.open() as factory:
        widget_service = await factory.create_widget_service()
        ...  # the nightly job, the notebook cell, the script body
    # everything opened via _enter / _aenter is closed here


asyncio.run(main())
```

Same factory, same `create_*` methods, same teardown guarantees — no app, routes, or
server. (In sync *test* code, use `FactoryHarness` — the sync bridge over `open()` —
see below.)

### The test seam

`BaseApp` accepts a prebuilt factory via `factory=`. That's the boundary tests use —
inject a stand-in factory so the real services are never constructed:

```python
app = App(factory=mock_factory)
```

See [Testing](testing.md) for the full pattern, including `FactoryHarness` for
exercising a real factory's wiring in isolation.

## Per-request resources

Lifecycle bound to a single request is just an `async with` inside the handler — no
framework machinery needed:

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

Past lifecycle, there's nothing to "resolve" — a dependency is a constructor argument,
and `wire` is where you pass it. Adding an injection/resolver system would buy
indirection, not capability. Don't reach for one.
