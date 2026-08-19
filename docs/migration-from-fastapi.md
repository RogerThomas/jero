# Migrating from FastAPI

Most FastAPI concepts have a direct jero equivalent — the shape changes, the idea
carries over. This page is the map. For *why* the shapes differ, read
[Comparison](comparison.md); for a routes-and-dependencies side-by-side, its
[FastAPI section](comparison.md#compared-with-fastapi).

## The map

| FastAPI | jero |
| --- | --- |
| `@app.get("/x")` on a function | An [`Endpoint`](guide/resources.md#endpoint-single-routes) class with a `get` method |
| A group of CRUD routes / `APIRouter` | A [`Resource`](guide/resources.md) class — method names are the operations |
| Pydantic `BaseModel` | msgspec `Struct` (below) |
| `Field(...)` constraints | `Annotated[..., msgspec.Meta(...)]` — [validated and documented](guide/openapi.md#schemas-and-msgspecmeta) |
| `Depends(...)` | Constructor arguments, wired in [`wire`](guide/wiring.md) |
| `Query()` / `Path()` / `Header()` / `Cookie()` params | `params` / `path` / `headers` / `cookies` [Structs](guide/binding.md) |
| `response.set_cookie(...)` / `response.delete_cookie(...)` | `SetCookie(...)` / `SetCookie.expire(...)` on any [response](guide/cookies.md) |
| `UploadFile`, `File()`, `Form()` | A `form` Struct with [`FilePart` / `FormPart`](guide/forms.md) |
| `HTTPException(404, detail=...)` | A typed [`HTTPError` subclass](guide/errors.md) you raise |
| `response_model=` | The return annotation — [it *is* the schema](guide/responses.md) |
| `status_code=201` | Automatic for `create`; otherwise a [response wrapper](guide/responses.md#status-codes) |
| `lifespan` / startup events | `wire` + [`_enter` / `_aenter`](guide/wiring.md#lifecycle-_enter-_aenter) |
| `BackgroundTasks` per request | One typed, app-wide [`BackgroundTasks` queue](guide/background-tasks.md) |
| ASGI middleware | The compiled [middleware protocol](guide/middleware.md) |
| `CORSMiddleware` | [`self._include_cors(CORS(...))`](guide/cors.md) |
| Auth dependency (`Depends(get_user)`) | An [authenticator](guide/auth.md) at the mount; handlers declare `user` |
| `fastapi.testclient.TestClient` | [`jero.testing.TestClient`](guide/testing.md) — also sync, in-process |
| `/docs` (automatic) | [`self._include_openapi(...)`](guide/openapi.md) — explicit, one line |

## Models: `BaseModel` → `Struct`

```python
# FastAPI / Pydantic
class Widget(BaseModel):
    name: str = Field(min_length=1)
    price_cents: int

    class Config:
        alias_generator = to_camel
```

```python
# jero / msgspec
from typing import Annotated
from msgspec import Meta, Struct


class Widget(Struct, rename="camel"):
    name: Annotated[str, Meta(min_length=1)]
    price_cents: int
```

Same contract: validated on decode, camelCase on the wire, constraints in the
[OpenAPI schema](guide/openapi.md#schemas-and-msgspecmeta). Give your project one base
Struct fixing the wire convention and inherit it everywhere.

## Errors: `HTTPException` → `HTTPError`

```python
# FastAPI
raise HTTPException(status_code=404, detail="widget not found")
```

```python
# jero
class WidgetNotFoundError(
    HTTPError,
    type="widget-not-found",
    title="Widget not found",
    status=404,
): ...


raise WidgetNotFoundError()
```

Four lines once, instead of a string at every raise site — clients dispatch on the
stable `type`, and the error [documents itself](guide/openapi.md#documenting-errors) in
the spec. [Errors](guide/errors.md) covers parameterized details and custom body
formats.

## What doesn't carry over

- **A DI container.** There is no `Depends`. Build objects in `wire`, pass them to
  constructors — [Wiring & lifecycle](guide/wiring.md) is the whole story.
- **`request.state`.** Middleware can't pass state to handlers. Bind the header you
  need in the handler, or carry it on the auth `user`.
- **Returning `dict`s.** A JSON body is a `Struct` — a `dict` return is a startup
  error, and that's what buys validation, schema, and speed.
- **Body-rewriting middleware.** Compression, caching, and ETags belong in your
  server or reverse proxy — see [Deployment](guide/deployment.md#what-belongs-in-the-proxy).
- **Static files.** Small ones, yes — [`_include_assets`](guide/assets.md) serves them
  from memory; large files and catch-all SPA fallbacks still belong on your proxy or CDN.

## Porting order that works

1. Port models to `Struct`s (mechanical, type-checker-guided).
1. Group routes into `Resource` / `Endpoint` classes; move `Depends` chains into
   constructors and `wire`.
1. Replace `HTTPException`s with typed errors.
1. Wire `_include_openapi`, boot the app, and fix each `WiringError` it reports —
   startup validation is the migration checklist running itself.
1. Point your test suite at `jero.testing.TestClient`.
