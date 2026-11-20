# Middleware

A middleware is a plain object with one or more hooks. There is no base class and no
decorator: jero reads the hooks at wiring and compiles only the ones the object defines
(the same pattern as [custom exception handlers](errors.md#custom-exception-handlers)).

```python
import logging

from jero import BaseApp, Endpoint, Request, Struct

logger = logging.getLogger("app.timing")


class Status(Struct):
    ok: bool


class TimingMiddleware:
    def observe(self, request: Request, status: int, duration: float) -> None:
        logger.info("%s %s -> %d in %.1fms", request.method, request.path, status, duration * 1e3)


class HealthEndpoint(Endpoint, path="/healthz"):
    def get(self) -> Status:
        return Status(ok=True)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_middleware(TimingMiddleware())
        self._include_endpoint(HealthEndpoint())


app = App()
```

A middleware can do three things:

- **`intercept`**: answer a request before routing and auth (gates, preflights).
- **`response_headers`**: add headers to responses, constant or computed.
- **`observe`**: see the outcome after the response is sent (timing, audit).

Two facts about `observe`, used above:

- It runs after the response is sent, sync or async, so it never adds latency a caller
  sees.
- `duration` is measured from dispatch to response start.

A route is **covered** by a middleware when the middleware is registered app-wide or
passed on that route's include (see [Registration](#registration-global-and-per-include)).
Routes no middleware covers are not wrapped and not branched into: they run the same
request path as an app without middleware.

## Registration: global and per-include

```python
from typing import ClassVar

from jero import BaseApp, Endpoint, Request, Struct


class ServerHeaders(Struct):
    x_served_by: str = "jero"


class BrandMiddleware:
    response_headers: ClassVar[ServerHeaders] = ServerHeaders()


class AuditMiddleware:
    def observe(self, request: Request, status: int, duration: float) -> None: ...


class Status(Struct):
    ok: bool


class HealthEndpoint(Endpoint, path="/healthz"):
    def get(self) -> Status:
        return Status(ok=True)


class MetricsEndpoint(Endpoint, path="/metrics"):
    def get(self) -> Status:
        return Status(ok=True)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_middleware(BrandMiddleware())      # every route
        self._include_endpoint(HealthEndpoint())         # covered by globals
        self._include_endpoint(
            MetricsEndpoint(),
            middleware=(AuditMiddleware(),),             # + include-scoped
        )


app = App()
```

- **`_include_middleware(...)`** registers a global middleware: it covers every route.
- **`middleware=(...)`** is an include-time keyword beside `auth=`: it covers only the
  routes of that include, on top of the globals.

Scope is deployment policy, so it is set at the mount and never on the
`Endpoint`/`Resource` class.

## The protocol

A middleware object may define any of these. Everything else on it is ignored, and an
object defining none of them is a `WiringError`.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from typing import ClassVar

from jero import HTTPMethod, JSONResponse, Request, Struct


class SecurityHeaders(Struct):
    x_frame_options: str = "DENY"


class RefusedBody(Struct):
    reason: str


class OriginHeaders(Struct):
    origin: str | None = None


class ExampleMiddleware:
    # EITHER a constant attribute (baked into covered routes at wiring, free) ...
    response_headers: ClassVar[SecurityHeaders] = SecurityHeaders()

    # static scope declaration: intercept compiles into these wire verbs only
    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    # answer instead of routing; None falls through. Sync or async.
    def intercept(self, request: Request[OriginHeaders]) -> JSONResponse[RefusedBody] | None: ...

    # observability: sees the outcome, cannot touch it. Exceptions logged + swallowed.
    def observe(self, request: Request, status: int, duration: float) -> None: ...
```

`response_headers` has two forms, and a class defines one or the other:

- **Constant**: a Struct attribute, as above.
- **Method**: `def response_headers(self, request: Request[H]) -> HeadersStruct | None`,
  called for every covered response.

### `Request`

Every hook receives a read-only `Request[H]`:

| Field         | Value |
| :------------ | :---- |
| `method`      | The wire method. A `HEAD` request reads `"HEAD"`, even though routing serves it from a `GET` handler. |
| `path`        | The request path. |
| `headers`     | *Your* Struct `H`, bound like [auth's `headers`](auth.md): wiring compiles a scanner for just the keys your fields name. A bare `Request` binds no headers. |
| `received_at` | `time.perf_counter()` at dispatch. Stamped on routes covered by a per-request hook, which is every hook except the constant `response_headers` attribute. |

### What middleware cannot do

- **Rewrite a handler's body or status.** That needs every response buffered or wrapped,
  and belongs in the server or your reverse proxy (compression, caching, ETags).
- **Pass state to handlers** (`request.state.tenant`). Use jero's typed sources instead:
  bind the header in the handler, or carry the value on the auth `user`.
- **Emit `content-length` or `content-type`.** The senders own those.

## The tiers and what they cost

| hook | when it runs | per-request cost |
| :-- | :-- | :-- |
| `response_headers` (constant attribute) | never: baked into route header blocks at wiring | zero |
| `intercept` + `intercept_methods` | only on the declared verbs, pre-auth | zero off-scope; one table hit + scan on-scope |
| `response_headers(...)` (method) | every covered response, as it leaves | one scan + call + merge |
| `observe(...)` | after the response is sent | request view + one call |

So:

- Use the constant attribute when a header value is fixed.
- Scope an intercept to the verbs that need it.
- Use the method form of `response_headers` only when the value depends on the request,
  because it runs on every covered response.

Every cost is opt-in and paid only by covered routes.
[Measured numbers](../performance.md#what-middleware-and-cors-cost) are on the
Performance page.

## The request path

The order is fixed:

```
request arrives
  1. global intercepts (verb-scoped, registration order) — first response wins
  2. routing (static hit / dynamic match; 404/405/OPTIONS fallthrough)
  3. include-scoped intercepts, same rule
  4. auth → bind → handler
  5. response_headers merges onto WHATEVER response leaves
     (handler success, problem body, exception-handler response, short-circuit)
  6. observe sees the outcome after the response is sent
```

What follows from it:

- **Global intercepts run before routing**, so they can answer requests no route serves.
  That is how an OPTIONS-scoped intercept answers preflights for paths that would 404.
- **Intercepts run before auth.** A preflight carries no credentials.
- **Intercept scoping sees the wire method.** `HEAD` is its own entry, even though
  routing serves it from `GET` handlers.

Errors and conflicts:

- An exception inside `intercept` or `response_headers` enters the same funnel as a
  handler exception: custom handlers get their shot, then the generic 500 problem.
- An exception inside `observe` is logged and swallowed.
- Two middlewares whose *constant* pairs claim the same header name are a `WiringError`.
  Computed pairs can't be checked, and append per HTTP semantics.

## An interception gate

An admin include gated on a header. The gate is scoped at the mount, so the rest of the
app never pays for it:

```python
from typing import ClassVar

from jero import BaseApp, Endpoint, HTTPMethod, JSONResponse, Request, Struct


class GateHeaders(Struct):
    x_admin_key: str | None = None


class Refused(Struct):
    reason: str


class AdminGateMiddleware:
    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET", "POST")

    def intercept(self, request: Request[GateHeaders]) -> JSONResponse[Refused] | None:
        if request.headers.x_admin_key != "letmein":
            return JSONResponse(json=Refused(reason="admin key required"), status_code=403)
        return None  # fall through to auth → bind → handler


class Report(Struct):
    widgets: int


class AdminReportEndpoint(Endpoint, path="/admin/report"):
    def get(self) -> Report:
        return Report(widgets=42)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(AdminReportEndpoint(), middleware=(AdminGateMiddleware(),))


app = App()
```

An intercept's return annotation follows handler rules:

- Any buffered response kind, unioned freely. `| None` means "fall through".
- A plain `Struct` answers with 200. `NoContent` / `Created` / `Accepted` fix their own
  status. `status_code=` overrides.
- Streaming kinds are a `WiringError`: an interception is an answer, not a stream.

## Rebuilding CORS yourself

The built-in [`CORS`](cors.md) policy uses exactly this machinery: nothing it does is
privileged. A wildcard policy is one constant attribute plus one preflight intercept:

```python
from typing import ClassVar

from jero import BaseApp, Endpoint, HTTPMethod, NoContent, Request, Struct


class WildcardCORSHeaders(Struct):
    access_control_allow_origin: str = "*"


class PreflightHeaders(Struct):
    access_control_request_method: str | None = None


class PreflightResponseHeaders(Struct):
    access_control_allow_origin: str = "*"
    access_control_allow_methods: str = "GET, POST, PUT, PATCH, DELETE"
    access_control_allow_headers: str = "content-type, authorization"
    access_control_max_age: int = 600


class HandRolledCORS:
    """The wildcard tier: constant pairs on every response, preflights answered."""

    response_headers: ClassVar[WildcardCORSHeaders] = WildcardCORSHeaders()

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("OPTIONS",)

    def intercept(
        self, request: Request[PreflightHeaders]
    ) -> NoContent[PreflightResponseHeaders] | None:
        if request.headers.access_control_request_method is None:
            return None  # a plain OPTIONS, not a preflight — let the Allow answer run
        return NoContent(headers=PreflightResponseHeaders())


class Status(Struct):
    ok: bool


class HealthEndpoint(Endpoint, path="/healthz"):
    def get(self) -> Status:
        return Status(ok=True)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_middleware(HandRolledCORS())
        self._include_endpoint(HealthEndpoint())


app = App()
```

Registered globally, the intercept answers preflights before routing, including for
paths that would 404, and the constant pair rides every response for free.

Prefer the built-in (`self._include_cors(CORS())`): it also handles origin allow-lists,
per-route preflight policy, and validation. But when you need a cross-cutting behavior
jero doesn't ship, this is the shape it takes.

## Why not onion middleware

The classic ASGI shape, an onion of app wrappers around `send`, costs every request on
every layer whether or not the layer applies: one layer that only appends a constant
header measured about **1.3×** on jero's hot path. See the
[measured per-tier numbers](../performance.md#what-middleware-and-cors-cost).
