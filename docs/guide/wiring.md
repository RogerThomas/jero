# Wiring & lifecycle

jero has **no DI container** — and that's deliberate, not a gap. You hand-wire classes
in `_wire`; a dependency is just a constructor argument. The one thing plain Python
doesn't give you for free — resource *lifecycle* — is the only thing the framework
adds.

## `_wire`

Subclass `BaseApp` and override `_wire`. It runs once at startup; here you construct
services and register routes. It's linear `async` code — no `yield`, no magic:

```python
class App(BaseApp):
    async def _wire(self) -> None:
        service = WidgetService(...)
        self._include_resource(WidgetResource(service), path="/widgets")
```

A resource's dependencies are constructor arguments you pass in. Want to share a
service across resources? Build it once and pass it to each.

## Lifecycle: `_enter` / `_aenter`

Resources that must be opened and closed — HTTP clients, DB pools — are entered on the
app's exit stacks. The app owns a sync `ExitStack` and an `AsyncExitStack` and closes
everything in reverse order at shutdown, even if `_wire` fails partway:

```python
class App(BaseApp):
    async def _wire(self) -> None:
        client = await self._aenter(httpx.AsyncClient())   # closed at shutdown
        cache = self._enter(open_cache())                  # sync context manager
        self._include_resource(WidgetResource(client, cache), path="/widgets")
```

`_aenter(cm)` enters an async context manager; `_enter(cm)` a sync one. Both return the
opened resource and register it for teardown.

## Factories

For anything real, group construction in a `BaseFactory`. Parameterize the app with it
— `BaseApp[Factory]` — and jero builds the factory at startup, injecting the exit
stacks. It's then `self._factory` inside `_wire`:

```python
from jero import BaseApp, BaseFactory


class Factory(BaseFactory):
    async def create_widget_service(self) -> WidgetService:
        client = await self._aenter(httpx.AsyncClient(base_url="https://api.example.com"))
        return WidgetService(client)


class App(BaseApp[Factory]):
    async def _wire(self) -> None:
        widgets = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(widgets), path="/widgets")
```

The factory's `create_*` methods use the same `_enter` / `_aenter` helpers — anything
they open is closed when the app shuts down. The split is a useful seam: the factory
owns the I/O services (the things with lifecycle), while pure in-memory wiring (an auth
token map, say) can live directly in `_wire`.

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
async def create(self, json: WidgetIn) -> Widget:
    async with open_txn() as txn:
        return await txn.insert(json)
```

## Why no resolver

Past lifecycle, there's nothing to "resolve" — a dependency is a constructor argument,
and `_wire` is where you pass it. Adding an injection/resolver system would buy
indirection, not capability. Don't reach for one.
