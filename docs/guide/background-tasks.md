# Background tasks

For work that shouldn't block the response — emitting analytics, warming a cache,
sending a best-effort notification — drop a `Struct` on a `BackgroundTasks` queue. A
single worker pulls each item and runs the handler registered for its type.

`BackgroundTasks` is **in-process and fire-and-forget**: items live in memory and are
lost on crash or restart. It is *not* a durable broker — for must-run work, use a real
queue (Celery, a message bus, your database etc.).

## A complete example

`BackgroundTasks` is a normal dependency: build it in `wire`, open it with `aenter`
(it's an async context manager, so the worker starts at startup and drains at shutdown),
register your handlers, and inject it into the endpoints that enqueue.

```python
import asyncio
from dataclasses import dataclass

from msgspec import Struct

from jero import BackgroundTasks, BaseApp, BaseFactory, Endpoint


class AnalyticsEvent(Struct):
    name: str


@dataclass
class AnalyticsService:
    async def process(self, event: AnalyticsEvent) -> None:
        # slow work, off the request path: write to a warehouse, call an API, ...
        await asyncio.sleep(0)


@dataclass
class EventsEndpoint(Endpoint, path="/events"):
    _background_tasks: BackgroundTasks

    async def post(self, json: AnalyticsEvent) -> AnalyticsEvent:
        await self._background_tasks.add(json)   # returns immediately; processed in the background
        return json


class Factory(BaseFactory):
    async def create_analytics_service(self) -> AnalyticsService:
        return AnalyticsService()


class App(BaseApp[Factory]):
    async def wire(self) -> None:
        analytics = await self.factory.create_analytics_service()
        tasks = await self.aenter(BackgroundTasks(drain_timeout=30.0))
        tasks.register(analytics.process)   # the item type is inferred from the handler
        self.include_endpoint(EventsEndpoint(tasks))


app = App()
```

## Registering handlers

`register(handler)` takes the handler alone — the item type is **inferred from its single
parameter**. The signature is the contract, exactly as it is for request binding and
responses: a handler takes one argument, annotated with the `Struct` it processes.

A handler that doesn't have exactly one parameter annotated with a `Struct` subclass is a
`WiringError` at startup. Dispatch is by **exact type** (`type(item)`) — a subclass is a
different key.

By default there is **one handler per type**; registering a second for the same `Struct`
is a `WiringError`. To fan out — run several handlers for one event — opt in:

```python
BackgroundTasks(drain_timeout=30.0, allow_one_to_many=True)
```

All handlers for a type then run sequentially, in registration order, each isolated so one
failure doesn't skip the rest.

## Enqueuing

`await tasks.add(item)` from anywhere holding the queue. The queue is **bounded** (default
`maxsize=1024`); when it's full, `add` waits — backpressure flows into the request rather
than letting the queue grow without limit.

A handler raising is caught and logged; the worker survives and keeps processing. An item
whose type has no handler is logged and dropped.

## Shutdown — `drain_timeout`

The one policy knob controls what happens to queued work at shutdown:

- `drain_timeout=30.0` (a float) — drain best-effort for up to N seconds, then cancel and
  log whatever's left. Draining is best-effort, not guaranteed.
- `drain_timeout=None` — don't wait; cancel immediately and drop anything queued.

There's no "wait forever," so shutdown can't hang.

## Ordering: enter the queue last

Handlers run during the drain, and they use the services you built in `wire` (a DB pool,
an HTTP client) — which are torn down at shutdown in reverse order. So **enter the queue
after the resources its handlers use**, so the drain finishes before those resources
close. This falls out naturally: `register` needs the handler, which needs its service,
so the service is created (and entered) first.
