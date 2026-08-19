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
| Dependency model | Constructor wiring in `wire`; no DI container | Dependency injection system | Framework services / injection features | Dependency injection system |
| Validation model | msgspec `Struct`s | Pydantic / typing based | Built-in binders (Pydantic optional) | Pydantic, msgspec, attrs, dataclasses, and others |
| JSON request bodies | `Struct` only | Model or compatible body types | Multiple supported styles | Multiple supported styles |
| JSON responses | `Struct`, `list[Struct]`, or typed response wrappers | Broad return support | Broad return support | Broad return support |
| Startup validation | Aggressive wiring checks before serving | Some checks at startup, many errors remain request-dependent | Some checks at startup | Broad configuration and route validation |
| OpenAPI | Built in, derived from typed contracts | Built in | Supported | Built in |
| Philosophy | Narrow and opinionated | Ergonomic and broadly useful | Performance-oriented and flexible | Feature-rich and structured |

## Compared with FastAPI

FastAPI is the obvious reference point for modern Python API development. It made
typed handler signatures mainstream, brought excellent OpenAPI generation to everyday
Python services, and set a high bar for developer experience.

jero differs most sharply in how much freedom it allows. The same two operations, side
by side — FastAPI's decorators and `Depends`:

```python
class WidgetIn(BaseModel):
    name: str


class Widget(WidgetIn):
    id: str


@app.post("/widgets", status_code=201)
async def create_widget(
    widget: WidgetIn, service: WidgetService = Depends(get_service)
) -> Widget:
    return await service.create(widget)


@app.get("/widgets/{widget_id}")
async def read_widget(
    widget_id: str, service: WidgetService = Depends(get_service)
) -> Widget:
    return await service.get(widget_id)
```

jero's class, method names, and constructor:

```python
class WidgetIn(Struct):
    name: str


class Widget(WidgetIn):
    id: str


class WidgetPath(Struct):
    widget_id: str


@dataclass
class WidgetResource(Resource, path="/widgets"):
    _service: WidgetService

    async def create(self, json: WidgetIn) -> Widget:      # POST /widgets -> 201
        return await self._service.create(json)

    async def read_one(self, path: WidgetPath) -> Widget:  # GET /widgets/{widget_id}
        return await self._service.get(path.widget_id)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource(WidgetService()))
```

The deeper difference shows up when something is wrong. Rename FastAPI's `widget_id`
parameter and the framework silently reinterprets it as a required *query* parameter —
discovered when the route is first called. In jero, a `path` Struct whose fields don't
match the template — or a handler returning a raw `dict` — is a `WiringError`: the app
refuses to boot.

FastAPI also has a powerful dependency system. jero does not. In jero,
dependencies are constructor arguments, and `wire` is where objects are built and
registered. That loses some convenience and some plugin-style flexibility, but it makes
the application graph explicit and keeps framework work out of the request path.

The other major difference is the model layer. FastAPI is built around Pydantic. jero
is built around msgspec `Struct`s for performance and for a single strict contract
shape.

Choose FastAPI when you want the ecosystem, the broadest familiarity, rich OpenAPI
support today, and flexible dependency ergonomics. Choose jero when you want a stricter
msgspec-first design with class resources, explicit wiring, aggressive startup
validation, and roughly 2x FastAPI's throughput on the same workloads (see
[Performance / Benchmarks](performance.md)).

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

jero goes in the opposite direction. There is one body model: `Struct`. There is one routing shape for REST
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

It is the right tradeoff if you want the framework to give a clear answer and hold you
to it.
