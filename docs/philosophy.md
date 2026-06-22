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

## No decorators

Route decorators are the common, well-understood default, and what most frameworks reach
for:

```python
@app.get("/widgets/{widget_id}")
async def read_widget(widget_id: str) -> Widget:
    ...
```

It's a simple approach with a real merit: for a single handler, the path, the verb, and
the function sit together and read cleanly. But it carries tradeoffs. They surface not
in any one route, but in how a whole API is *organized*, and above all in how handlers
get their dependencies.

A plain function carries no state of its own. With no `self` to hold the collaborators a
handler depends on (a database pool, an HTTP client, a service, etc.), those dependencies have
to be supplied some other way, and there are really only two options: reach for
module-level singletons and globals (the Flask `current_app` / `g` / app-bound-extension
pattern), or adopt a framework-specific dependency-injection system (for example
FastAPI's [`Depends`](https://fastapi.tiangolo.com/tutorial/dependencies/) or Litestar's
[`Provide`](https://docs.litestar.dev/latest/usage/dependency-injection.html)). Both
exist to thread state into functions that can't hold it themselves.

The grouping is loose for the same reason. The operations that belong together (create,
read, list, update, delete on one collection) are separate functions that share a path
prefix and a module; nothing *is* the collection. A router to group routes and a DI layer
to feed them are, in large part, recovering what a plain class would give you for nothing.

jero starts from that class. A route is a `Resource` for a REST collection, or an
`Endpoint` for a one-off route. The path lives on the class and method names carry the
HTTP semantics, while dependencies are ordinary constructor arguments:

```python
@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    async def read_one(self, path: WidgetPath) -> Widget:
        return await self._service.get_widget(path.widget_id)
```

Now the collection is one object. Its operations live together because they *are*
together; its dependencies are passed to `__init__`, so there are no globals and no DI
container to learn; and it's a stable thing the framework can attach to: a shape to
validate at startup, the target for reverse-routed `Location` / `Link` headers, and the
anchor for OpenAPI generation. jero doesn't give you a better dependency-injection
system. It removes the need for one.

None of this makes decorators wrong. They're lighter for a handful of one-off routes,
and they're what most people reach for first. jero takes the other side of the trade:
for a typed, REST-shaped JSON API, a class can serve as a better unit of design.

## No dependency injection container

As the previous section noted, decorator routing nudges you toward a dependency-injection
system to feed standalone functions. jero needs none. In the author's opinion, those
containers are often needlessly complicated in Python web applications.

Python already has a dependency injection mechanism: constructors. Build an object and
pass it the things it needs. That approach is explicit, type-checkable, easy to debug,
and easy to test. jero keeps that model instead of adding a resolver layer.

The app's `_wire` method is ordinary async Python:

```python
class App(BaseApp[Factory]):
    async def _wire(self) -> None:
        service = await self._factory.create_widget_service()
        self._include_resource(WidgetResource(service))
```

The framework does add one thing plain Python does not give you by default: lifecycle.
If an object needs to be opened and closed for the app lifetime, enter it with `_enter`
or `_aenter`. The app owns the exit stacks and shuts resources down in reverse order.
For larger apps, a `BaseFactory` groups construction and still uses the same explicit
constructor style.

There is no hidden resolver graph, no per-request container lookup, no dependency
function protocol to learn, and no framework-specific indirection between a class and
its collaborators.

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

Because a route is a class, the method *names* can carry the REST semantics directly.
`create` is POST on the collection; `read_many` is GET on the collection; `read_one`,
`update`, `partial_update`, and `delete` are the item operations. The table is small
enough to learn quickly and strict enough for the framework to enforce at startup.

The result is less freedom, but more shape. jero is comfortable with that tradeoff.

## Opinionated by design

Opinionated does not mean "arbitrary." It means the framework has an answer.

How do I define routes? Use `Resource` or `Endpoint`.

How do I bind request data? Name the handler argument `json`, `params`, `path`,
`headers`, `form`, `content`, `raw_headers`, or `user`.

How do I return JSON? Return a `Struct`, `list[Struct]`, or `JSONResponse[T, H]`.

How do I wire dependencies? Construct them in `_wire` or a `BaseFactory`, then pass
them to class constructors.

How do I manage app lifetime resources? Use `_enter` and `_aenter`.

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
