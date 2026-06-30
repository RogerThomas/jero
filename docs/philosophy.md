# Philosophy

jero exists because the current Python web framework defaults are not the only way to
build APIs.

FastAPI, BlackSheep, Litestar, Django, Flask, Starlette, and the rest have all pushed
Python web development forward. jero is not a rejection of that work. It is a different
set of bets: fewer extension points, fewer runtime decisions, stronger static
contracts, and a narrower idea of what a JSON API framework should be.

The short version: jero gives you one framework answer to common API questions. Don't
fight the framework. Trust the shape for a while, build something real, and see whether
the tradeoff pays for itself.

## No decorators, no DI container

Route decorators are the common default, and what most frameworks reach for:

```python
@app.get("/widgets/{widget_id}")
async def read_widget(widget_id: str) -> Widget:
    ...
```

For a single handler this reads cleanly: path, verb, and function sit together. But it
carries tradeoffs, and they surface not in any one route but across a whole API: in how
operations are grouped, and above all in how handlers get their dependencies. A plain
function carries no state, so with no `self` to hold what a
handler depends on (a database pool, an HTTP client, a service), those dependencies must
arrive some other way. There are only two: module-level globals (Flask's `current_app` /
`g`), or a framework-specific dependency-injection system (FastAPI's
[`Depends`](https://fastapi.tiangolo.com/tutorial/dependencies/), Litestar's
[`Provide`](https://docs.litestar.dev/latest/usage/dependency-injection.html), to name but
a few). Grouping
is loose for the same reason: the operations on one collection are separate functions
sharing a path prefix, but nothing *is* the collection. A router to group routes and a DI
layer to feed them are largely recovering what a plain class gives for free.

jero starts from that class. A route is a `Resource` for a REST collection or an
`Endpoint` for a one-off route; the path lives on the class, method names carry the HTTP
semantics, and dependencies are ordinary constructor arguments:

```python
@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._service.get_widget(path.widget_id)
```

The collection is now one object whose operations live together because they *are*
together, and whose dependencies are passed to `__init__`. That is the whole dependency
story: Python's own injection mechanism, constructors. Build an object, pass it what it
needs. No globals, no resolver graph, no per-request container lookup, no dependency
protocol to learn. Wiring is ordinary async Python:

```python
class App(BaseApp[Factory]):
    async def wire(self) -> None:
        service = await self.factory.create_widget_service()
        self.include_resource(WidgetResource(service))
```

The framework adds one thing plain Python does not: lifecycle. Enter a resource that must
be opened and closed with `enter` or `aenter`, and the app closes it in reverse order at
shutdown; for larger apps a `BaseFactory` groups construction in the same explicit style.

None of this makes decorators wrong. They are lighter for a handful of one-off routes.
jero takes the other side: for a typed, REST-shaped JSON API, a class can be the better
unit of design, and it doubles as a stable thing the framework attaches to: a shape to
validate at startup, the target for reverse-routed `Location` / `Link` headers, and the
anchor for OpenAPI generation.

## msgspec first

jero is built on msgspec for performance.

The framework's hot path is intentionally small: route lookup, msgspec decode, handler
call, msgspec encode. msgspec's `Struct` types give jero fast validation and
serialization without translating between separate framework models and wire models.
That matters because JSON APIs spend a lot of time turning bytes into objects and
objects back into bytes.

Pydantic is excellent software and has had an enormous influence on Python API
development. jero makes a different bet: if the framework is going to be strict,
typed, and JSON-focused, msgspec is the better foundation for the performance profile
jero is trying to hit.

## Struct everywhere

In jero, JSON request bodies and JSON responses are `Struct`s. Query params, path
params, typed headers, form models, auth users, and typed response headers are also
`Struct`s.

That is not ceremony for its own sake. The type is the contract.

When a handler accepts `json: WidgetIn`, jero knows the request body shape. When it
returns `Widget`, jero knows the response body shape. That single source drives
validation, serialization, startup checks, and the coming OpenAPI generator. A raw
`dict` does not carry enough information. It may be convenient in the moment, but it
turns the framework blind at exactly the boundary where the contract matters most.

The rule is intentionally strict. JSON is typed or it is rejected at startup.

## Startup validation

jero tries hard to fail before the app serves traffic.

If a route's path slots don't match its `path` Struct, that is a startup error. If a
handler declares `user` without auth, that is a startup error. If auth returns one user
type and the handler asks for another, that is a startup error. If a response type
cannot be understood as a framework response contract, that is a startup error.

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

This is the best of both worlds. The project picks a single fast checker for its own
source, and at the same time guarantees that, whatever your favourite type checker is,
jero's public API is fully supported and type-checks cleanly under it.

## Class-based resources

The method *names* carry the REST semantics directly: `create` is POST on the collection,
`read_many` is GET on the collection, and `read_one`, `update`, `partial_update`, and
`delete` are the item operations. The set is small enough to learn quickly and strict
enough for the framework to enforce at startup.

Less freedom, but more shape. jero is comfortable with that tradeoff.

## Opinionated by design

Opinionated does not mean "arbitrary." It means the framework has an answer.

How do I define routes? Use `Resource` or `Endpoint`.

How do I bind request data? Name the handler argument `json`, `params`, `path`,
`headers`, `form`, `content`, `raw_headers`, or `user`.

How do I return JSON? Return a `Struct`, `list[Struct]`, or `JSONResponse[T, H]`.

How do I wire dependencies? Construct them in `wire` or a `BaseFactory`, then pass
them to class constructors.

How do I manage app lifetime resources? Use `enter` and `aenter`.

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

jero is narrow on purpose. That is the point.
