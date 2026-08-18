# FAQ

## Can handlers be sync?

Yes. Handlers, `authenticate`, and middleware hooks may all be `def` or `async def` —
jero detects which at wiring. Use sync for pure CPU-light work; anything that does I/O
should be async.

## Does jero support WebSockets?

Yes—jero provides typed bidirectional [WebSockets](guide/websockets.md) and an
in-process typed `Channel` for fan-out. For one-way server push, prefer
[NDJSON or Server-Sent Events](guide/streaming.md), which cover most live-update cases
and give SSE clients automatic reconnection.

## Can I serve static files?

Small ones, yes: [`_include_assets`](guide/assets.md) reads a directory once at wiring
and serves it from memory, with `ETag`, gzip, and cache headers all baked at startup.
Real static serving (large files, `Range` requests, catch-all SPA fallbacks) still
belongs on your reverse proxy or CDN (see
[Deployment](guide/deployment.md#what-belongs-in-the-proxy)).

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

## How stable is the API?

jero is in beta, on the 0.x series. The public surface is settled and fully documented,
but a breaking change may still land in a minor release until 1.0 — every release's
changes are listed in the [release notes](https://github.com/RogerThomas/jero/releases).

## Why is the `TestClient` sync?

So plain `pytest` functions work with no async plumbing: it runs the app's full
lifespan on a background event loop and gives you a `requests`-style API. See
[Testing](guide/testing.md).
