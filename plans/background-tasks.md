# Background tasks

In-process, fire-and-forget background work. Endpoints drop a `Struct` on a queue; a
single worker dispatches each item to the handler registered for its type.

**Non-goal:** durability. Items are lost on crash/restart. This is not a broker — use a
real queue for must-run work.

## Shape

`BackgroundTasks` — exported by jero. Not a framework flag; just a service the user's
factory builds and `_aenter`s, like any other dependency.

- `register[T: Struct](handler: Callable[[T], Awaitable[None]])` — wire-time. The item
  type is **inferred** from the handler's single parameter (the signature is the
  contract, same as binding/responses), resolved to the real `Struct` class and used as
  the map key. Fail loud (`WiringError`) if the handler doesn't have exactly one
  annotated `Struct` param.
- **One handler per type by default:** a second handler for the same `Struct` →
  `WiringError`. Opt into fan-out with `create_background_tasks(allow_one_to_many=True)`;
  the map value becomes a list and the worker runs **all** handlers for the type
  sequentially, in registration order, with errors isolated per handler.
- `async add(item: Struct)` — request-time. Bounded queue; awaits when full
  (backpressure into the request).
- Async context manager: `__aenter__` starts the one worker; `__aexit__` drains then
  cancels.

Both `register` and `add` are public on the one class. (Optional later: a
`BackgroundQueue` Protocol exposing only `add`, for endpoints to type against.)

Worker: single, serial. Dispatch by `type(item)`. A handler raising is caught + logged;
the worker survives. Unknown type → logged + dropped.

## Lifecycle

Built in `_wire` via the factory and `_aenter`'d, so start/stop ride the existing exit
stack — no changes to `_handle_lifespan`.

**Ordering rule (document it):** enter the queue *after* the resources its handlers use,
so reverse-order shutdown drains it *before* those resources are torn down. Falls out
naturally — `register` needs the handler, which needs its service, created first.

## Shutdown — `drain_timeout: float | None`

The only policy knob, on the constructor:

- `None` → don't wait; cancel now, drop queued items.
- `float` → drain best-effort up to N seconds, then cancel and **log** the remainder.

No "wait forever" — shutdown can't hang.

## Wiring (sketch)

```python
async def _wire(self) -> None:
    analytics = await self._factory.create_analytics_event_handler()
    tasks = await self._factory.create_background_tasks()
    tasks.register(analytics.process_item)  # item type inferred from the handler
    self._include_endpoint(EventsEndpoint(tasks), path="/events")
```
