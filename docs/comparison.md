# Comparison

jero was inspired by the Python frameworks that came before it. FastAPI showed how much
better API development could feel with type annotations. BlackSheep showed that Python
frameworks could be very fast. Litestar showed the value of a more structured,
production-minded framework surface.

jero takes lessons from all of them, then makes different tradeoffs. It is not trying
to be a drop-in replacement for any one framework. It is a smaller, stricter,
msgspec-first ASGI framework for typed JSON APIs.

## At a glance

| Area | jero | FastAPI | BlackSheep | Litestar |
| ---- | ---- | ------- | ---------- | -------- |
| Route style | Class-based `Resource` / `Endpoint` | Decorator functions | Decorators and controller-style APIs | Decorators and controller-style APIs |
| REST resources | First-class CRUD method names | User-defined routes | User-defined routes/controllers | User-defined routes/controllers |
| Dependency model | Constructor wiring in `_wire`; no DI container | Dependency injection system | Framework services / injection features | Dependency injection system |
| Validation model | msgspec `Struct`s | Pydantic / typing based | Framework validation options | Pydantic, msgspec, attrs, dataclasses, and others |
| JSON request bodies | `Struct` only | Model or compatible body types | Multiple supported styles | Multiple supported styles |
| JSON responses | `Struct`, `list[Struct]`, or typed response wrappers | Broad return support | Broad return support | Broad return support |
| Startup validation | Aggressive wiring checks before serving | Some checks at startup, many errors remain request-dependent | Framework-dependent | Broad configuration and route validation |
| Runtime introspection | Avoided on the request path | More dynamic | Depends on feature | Depends on feature |
| OpenAPI direction | Derived from typed contracts | Built in | Supported | Built in |
| Philosophy | Narrow and opinionated | Ergonomic and broadly useful | Performance-oriented and flexible | Feature-rich and structured |

This table is necessarily simplified. Each framework has more nuance than a matrix can
show, and the right choice depends on the application.

## Compared with FastAPI

FastAPI is the obvious reference point for modern Python API development. It made
typed handler signatures mainstream, brought excellent OpenAPI generation to everyday
Python services, and set a high bar for developer experience.

jero differs most sharply in how much freedom it allows. FastAPI's decorator model is
flexible and familiar:

```python
@app.get("/widgets/{widget_id}")
async def read_widget(widget_id: str) -> Widget:
    ...
```

jero uses classes and method names:

```python
class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:
        ...
```

FastAPI also has a powerful dependency system. jero deliberately does not. In jero,
dependencies are constructor arguments, and `_wire` is where objects are built and
registered. That loses some convenience and some plugin-style flexibility, but it makes
the application graph explicit and keeps framework work out of the request path.

The other major difference is the model layer. FastAPI is built around Pydantic. jero
is built around msgspec `Struct`s for performance and for a single strict contract
shape.

Choose FastAPI when you want the ecosystem, the broadest familiarity, rich OpenAPI
support today, and flexible dependency ergonomics. Choose jero when you want a stricter
msgspec-first design with class resources, explicit wiring, and aggressive startup
validation.

## Compared with BlackSheep

BlackSheep is one of the frameworks jero looks up to on performance. It has shown that
Python ASGI frameworks can be extremely fast while still feeling productive.

jero's difference is not simply "faster" or "slower"; benchmark numbers depend on the
workload. The bigger difference is philosophy. jero narrows the API surface around
typed `Struct` contracts, class-based resources, no container, and startup validation.
It gives up some flexibility to make the framework style harder to miss.

Choose BlackSheep when you want a mature, high-performance ASGI framework with a
broader application model. Choose jero when you want a narrower REST/JSON framework
where the type contract drives almost everything.

## Compared with Litestar

Litestar is a capable, structured framework with a large feature surface. It supports
multiple validation backends and gives teams a lot of ways to model an application.

jero goes in the opposite direction. It intentionally avoids supporting many equivalent
styles. There is one body model: `Struct`. There is one routing shape for REST
collections: `Resource`. There is one dependency story: construct objects and pass them
in. There is one lifecycle mechanism: enter resources on the app's stacks.

Choose Litestar when you want a production framework with a broad feature set and more
configuration choices. Choose jero when you want fewer choices, stronger conventions,
and a smaller request path.

## The real tradeoff

jero is not trying to beat every framework on every axis. It is making a concentrated
tradeoff:

- Less routing flexibility, more route structure.
- Less dependency machinery, more explicit wiring.
- Less body/response permissiveness, stronger typed contracts.
- Less runtime discovery, more startup validation.
- Less framework surface, more predictable performance.

That tradeoff is not universally right. It is right if you want the framework to give a
clear answer and hold you to it.
