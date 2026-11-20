# Philosophy

FastAPI, BlackSheep, Litestar, Django, Flask, Starlette, and the rest have all pushed
Python web development forward. jero is not a rejection of that work. It is a different
set of bets: fewer extension points, fewer runtime decisions, stronger static
contracts, and a narrower idea of what a JSON API framework should be.

## No decorators, no DI container

Most frameworks reach for route decorators:

```python
@app.get("/widgets/{widget_id}")
async def read_widget(widget_id: str) -> Widget:
    ...
```

For a single handler this reads cleanly: path, verb, and function sit together. The
tradeoffs surface across a whole API.

The deepest one is dependencies. A plain function has no `self` to hold a database
pool, an HTTP client, or a service, so those must arrive some other way. There are only
two:

- module-level globals (Flask's `current_app` / `g`), or
- a framework-specific dependency-injection system (FastAPI's
  [`Depends`](https://fastapi.tiangolo.com/tutorial/dependencies/), Litestar's
  [`Provide`](https://docs.litestar.dev/latest/usage/dependency-injection.html)).

Grouping is loose for the same reason: the operations on one collection are separate
functions sharing a path prefix, but nothing *is* the collection. A router to group
routes and a DI layer to feed them are largely recovering what a plain class gives for
free.

jero starts from that class. A route is a `Resource` for a REST collection or an
`Endpoint` for a one-off route. The path lives on the class, method names carry the HTTP
semantics, and dependencies are ordinary constructor arguments:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._service.get_widget(path.widget_id)
```

The method names are a fixed set: `create` is POST on the collection, `read_many` is GET
on the collection, and `read_one`, `update_full`, `update_partial`, and `delete` are the
item operations. The set is small enough to learn quickly and strict enough for the
framework to enforce at startup. Less freedom, but more shape.

Dependencies use Python's own injection mechanism, constructors: build an object, pass
it what it needs. No globals, no resolver graph, no per-request container lookup, no
dependency protocol to learn. Wiring is ordinary async Python:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
class App(BaseApp[Factory]):
    async def wire(self) -> None:
        service = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(service))
```

The framework adds one thing plain Python does not: lifecycle. Enter a resource that must
be opened and closed with `_enter` or `_aenter`, and the app closes it in reverse order at
shutdown. For larger apps a `BaseFactory` groups construction in the same explicit style.

Decorators are lighter for a handful of one-off routes. For a typed, REST-shaped JSON
API, the class is the better unit of design. It is also a stable thing the framework
attaches to: a shape to validate at startup, the target for reverse-routed
`Location` / `Link` headers, and the anchor for OpenAPI generation.

## msgspec first

jero is built on msgspec for performance.

The hot path is small: route lookup, msgspec decode, handler call, msgspec encode.
msgspec's `Struct` types give jero fast validation and serialization without
translating between separate framework models and wire models. That matters because
JSON APIs spend a lot of time turning bytes into objects and objects back into bytes.

Pydantic shaped modern Python API development. jero bets differently: for a strict,
typed, JSON-focused framework, msgspec is the better foundation.

## Struct everywhere

JSON request bodies and JSON responses are `Struct`s. So are query params, path params,
typed headers, cookies, form models, typed response headers, etc. The auth user is the
one exception: it never crosses the wire, so it can be any type you like.

The type is the contract. When a handler accepts `json: WidgetIn`, jero knows the
request body shape. When it returns `Widget`, jero knows the response body shape. That
single source drives validation, serialization, startup checks, and the OpenAPI
generator.

A raw `dict` does not carry enough information. It may be convenient in the moment, but
it turns the framework blind at exactly the boundary where the contract matters most.
JSON is typed or it is rejected at startup.

## Startup validation

jero tries hard to fail before the app serves traffic. Each of these is a startup error:

- A route's path slots don't match its `path` Struct.
- A handler declares `user` without auth.
- Auth returns one user type and the handler asks for another.
- A handler's `user` annotation disagrees with whether its authenticator accepts
  anonymous callers, in either direction (including a handler that omits `user` on a
  route that serves them).
- A response type cannot be understood as a framework response contract.

This is a DX choice as much as a performance choice. Runtime flexibility often means
runtime surprise. jero would rather make invalid applications impossible to boot than
let the first unlucky request discover the problem.

Startup validation also protects the request path. All introspection happens once
during wiring. By the time a request arrives, the framework has already resolved the
route, binders, decoders, auth contract, response sender, and status behavior.

## Strictly typed, every checker

jero's core source is strictly type-checked with [pyrefly](https://pyrefly.org). On top
of that, the public-facing interface is checked with *every* major type checker:
[mypy](https://www.mypy-lang.org/), [ty](https://github.com/astral-sh/ty),
[pyright](https://microsoft.github.io/pyright/), and [zuban](https://zubanls.com). That
interface is everything jero's [test suite](guide/testing-approach.md) exercises: `./tests`
and the shared `demo_app` it runs against.

The project picks a single fast checker for its own source, and still guarantees that
jero's public API type-checks cleanly under whichever checker you prefer.

## Opinionated

Opinionated does not mean "arbitrary." It means the framework has an answer to each
"how do I...?": how to define routes, bind request data, return JSON, wire dependencies,
and manage app-lifetime resources. The answers are listed in
[The mental model](getting-started.md#the-mental-model).

This is the promise and the cost of jero. It is not a toolkit for assembling your own
framework style. It is a framework with a style. If you try to fight it, it will feel
too narrow. If you trust it, the reward is a smaller design space, earlier failures,
faster request handling, and fewer choices to relitigate in every codebase.

## Who jero is for

jero is for developers looking for a fresh, fast alternative to the current stalwarts:
FastAPI, BlackSheep, Litestar, and similar frameworks.

It is for teams that like strict typing, explicit wiring, msgspec, REST-shaped APIs,
startup validation, and framework conventions that remove debate. It is for people who
would rather have the framework say "this is how you do it" than expose five extension
points and let every project invent a local style.

It is not trying to be the most flexible Python web toolkit. It is not trying to make
dynamic JSON blobs feel effortless. It is not trying to hide Python behind a container.

jero is narrow. That is the point.
