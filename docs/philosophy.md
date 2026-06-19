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

Route decorators have become the accepted default in Python web frameworks:

```python
@app.get("/widgets/{widget_id}")
async def read_widget(widget_id: str) -> Widget:
    ...
```

That shape is familiar, but it has costs that are easy to ignore because everyone is
used to them.

A decorator route puts framework registration on a function as a side effect of import.
The function, path, HTTP verb, metadata, dependencies, and lifecycle story often spread
across multiple places. As an application grows, related operations become a loose set
of decorated functions that only look connected because they share a path prefix or live
near each other in a module. The framework then has to recover structure from a pile of
registered callables.

jero starts from the opposite direction. A route is a class. A REST collection is a
`Resource`; a one-off route is an `Endpoint`. The path lives on the class, and method
names carry the HTTP semantics:

```python
class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:
        ...
```

That gives the framework a stronger shape to validate at startup. It also gives the
developer a better unit of design. A resource can have constructor dependencies. It can
group the operations that belong together. It gives REST semantics a real home instead
of spreading them across decorator calls. It makes it obvious where CRUD behavior lives,
and it gives future features like generated links, `Location` headers, and OpenAPI
metadata a stable object to attach to.

## No dependency injection container

In the author's opinion, dependency injection containers are often needlessly
complicated in Python web applications.

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

## Class-based resources

Class-based resources are the center of jero's routing model because they give the API
a natural unit of organization and a simple dependency story.

A resource is a class with constructor arguments. That is the simplest form of
dependency injection: build the service, pass it to the resource, include the resource.
No container is needed.

The resource method names also encode REST semantics. `create` is POST on the
collection. `read_many` is GET on the collection. `read_one`, `update`,
`partial_update`, and `delete` are item operations. The method table is small enough to
learn quickly, and strict enough for the framework to enforce.

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
