# FAQ

## Can handlers be sync?

Yes. Handlers, `authenticate`, and middleware hooks may all be `def` or `async def` —
jero detects which at wiring. Use sync for pure CPU-light work; anything that does I/O
should be async.

## Does jero support WebSockets?

Not yet. jero streams one-way today — [NDJSON and Server-Sent Events](guide/streaming.md)
cover most live-update cases (and SSE reconnects for free). If you need bidirectional
sockets now, jero isn't your framework yet.

## Can I serve static files?

No. jero serves JSON APIs; put assets on your reverse proxy or CDN (see
[Deployment](guide/deployment.md#what-belongs-in-the-proxy)). The one exception is the
docs-page [favicon](guide/openapi.md#favicon), precomputed at wiring.

## Where is `request.state`?

There isn't one. Middleware can't pass state to handlers — the idiom is jero's typed
sources: [bind the header](guide/binding.md) in the handler, or carry the value on the
auth [`user`](guide/auth.md).

## Can I return a `dict`?

No — a JSON body is a `Struct` (or `list[Struct]`), and a `dict` return is a
`WiringError` at startup. The `Struct` is what buys validation, the OpenAPI schema, and
msgspec's encode speed. See [Philosophy](philosophy.md#struct-everywhere).

## Can I use Pydantic models?

Not on the wire — request and response contracts are msgspec `Struct`s. Off the wire,
anything goes: the [demo app](guide/project-structure.md) uses pydantic-settings to
parse environment configuration, then maps it into a `Struct`.

## Does it run on Python 3.12?

No. jero's generics use PEP 696 type-parameter defaults, which shipped in 3.13. See
[Getting started](getting-started.md#python-version).

## Why is there no dependency-injection container?

Because constructors already are one: build objects in `wire`, pass them in. The
framework adds only what plain Python lacks — resource lifecycle. See
[Wiring & lifecycle](guide/wiring.md) and [Philosophy](philosophy.md#no-decorators-no-di-container).

## How do I share state between workers?

You don't — each worker process runs `wire` and holds its own services and
[background queue](guide/background-tasks.md). Shared state (a cache, a durable queue)
belongs in an external service. See [Deployment](guide/deployment.md#running-the-app).

## Why is the `TestClient` sync?

So plain `pytest` functions work with no async plumbing: it runs the app's full
lifespan on a background event loop and gives you a `requests`-style API. See
[Testing](guide/testing.md).
