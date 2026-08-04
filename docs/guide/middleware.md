# Middleware

The classic ASGI middleware shape — an onion of app wrappers, each an async callable
wrapping `send` — costs a coroutine plus allocations per layer per request, on every
request, whether or not the layer applies. jero rejects that shape outright. Measured on
jero's hot path, one onion layer that does nothing but append a single constant CORS
pair already costs about **1.3×** — a third of the entire per-request budget, before it
does anything a real middleware does.

Instead, a jero middleware is a **typed, hand-wired object whose hooks are introspected
and compiled at wiring** (the same pattern as
[custom exception handlers](rest.md#custom-exception-handlers)): no base class, no
decorator. Wiring compiles only what a middleware actually defines, and each hook kind
has a fixed, known cost.

## The tiers and what they cost

| hook | when it runs | per-request cost |
| :-- | :-- | :-- |
| `response_headers` (constant attribute) | never — baked into route header blocks at wiring | zero |
| `intercept` + `intercept_methods` | only on the declared verbs, pre-auth | zero off-scope; one table hit + scan on-scope |
| `response_headers(...)` (method) | every covered response, as it leaves | one scan + call + merge |
| `observe(...)` | after the response starts | request view + one call |

Routes no middleware covers are not wrapped, not branched into, not touched — the
uncovered request path is byte-for-byte the one an app without middleware runs.

## The protocol

A middleware object may define any of these; everything else on it is ignored, and an
object defining none of them fails wiring loud:

```python
from typing import ClassVar

from jero import HTTPMethod, JSONResponse, Request, Struct


class SecurityHeaders(Struct):
    x_frame_options: str = "DENY"


class RefusedBody(Struct):
    reason: str


class OriginHeaders(Struct):
    origin: str | None = None


class ExampleMiddleware:
    # EITHER a constant attribute (baked into covered routes at wiring — free) ...
    response_headers: ClassVar[SecurityHeaders] = SecurityHeaders()

    # static scope declaration: intercept compiles into these wire verbs only
    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    # answer instead of routing; None falls through. Sync or async.
    def intercept(self, request: Request[OriginHeaders]) -> JSONResponse[RefusedBody] | None: ...

    # observability: sees the outcome, cannot touch it. Exceptions logged + swallowed.
    def observe(self, request: Request, status: int, duration: float) -> None: ...
```

(A middleware defines *either* the constant `response_headers` attribute *or* the method
form `def response_headers(self, request: Request[H]) -> HeadersStruct | None` — one
name, one meaning per class.)

`Request[H]` is the read-only view every hook receives: the wire `method` and `path`,
plus `headers` — *your* Struct, bound exactly like [auth's `headers`](auth.md): wiring
compiles a scanner for just the keys your fields name. Annotate a bare `Request` to bind
no headers at all. `received_at` is `time.perf_counter()` at dispatch, stamped on routes
a dynamic hook covers.

**The capability boundary is deliberate.** Middleware can *answer* a request
(`intercept`), *add response headers* (`response_headers`), and *watch* (`observe`). It
can never rewrite a handler's body or status: that requires buffering or wrapping every
response — the onion price — and belongs in granian or your reverse proxy (compression,
caching, ETags). jero refuses it on principle rather than covering it slowly. Passing
state from middleware to handlers (`request.state.tenant`) is also not offered: the
idiom is jero's existing typed sources — bind the header in the handler, or carry it on
the auth `user`.

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

`middleware=` is an include-time keyword beside `auth=` — middleware scope is deployment
policy, not resource identity, so it does not live on the `Endpoint`/`Resource` class.

The request path is fixed:

```
request arrives
  1. global intercepts (verb-scoped, registration order) — first response wins
  2. routing (static hit / dynamic match; 404/405/OPTIONS fallthrough)
  3. include-scoped intercepts, same rule
  4. auth → bind → handler
  5. response_headers merges onto WHATEVER response leaves
     (handler success, problem body, exception-handler response, short-circuit)
  6. observe sees the outcome after the response starts
```

Two consequences worth spelling out: **global intercepts run pre-routing**, so they can
answer requests no route serves (that's how an OPTIONS-scoped intercept answers
preflights for paths that would 404) — and **intercepts run before auth** (a preflight
carries no credentials). Intercept scoping sees the **wire method**: `HEAD` is its own
entry, even though routing serves it from `GET` handlers.

An exception inside `intercept` or `response_headers` enters the same funnel as handler
exceptions — custom handlers get their shot, then the generic 500 problem. `observe`
exceptions are logged and swallowed. Middleware can never emit `content-length` or
`content-type` (the senders own those), and two middlewares' *constant* pairs claiming
the same header name fail wiring loud; dynamic pairs can't be checked and append per
HTTP semantics.

## A complete example: request timing

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

`duration` is measured from dispatch to response-start. `observe` may be sync or async;
either way it runs after the response has left, so it never adds latency a caller sees.

## A complete example: an interception gate

An admin include gated on a header, scoped at the mount — the rest of the app never
pays for it:

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

An intercept's return annotation follows handler rules — any buffered response kind,
unioned freely, with `| None` meaning "fall through". A plain `Struct` return answers
with 200; `NoContent` / `Created` / `Accepted` fix their own status; `status_code=`
overrides. Streaming kinds are rejected at wiring: an interception is an answer, not a
stream.

## Rebuilding CORS yourself

The built-in [`CORS`](cors.md) policy uses exactly this machinery — nothing it does is
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

Registered globally, the intercept answers preflights pre-routing — including for paths
that would 404 — and the constant pair rides every response for free. Prefer the
built-in (`self._include_cors(CORS())`): it also handles origin allow-lists, per-route
preflight policy, and validation. But when you need a cross-cutting behavior jero
doesn't ship, this is the shape it takes.

## Measured cost

From the in-process hot-path benchmark (`bench.py`'s harness; POST echo, ~1.3µs/request
baseline — in-process numbers amplify framework deltas relative to a real server, where
socket I/O dominates):

| configuration | relative cost |
| :-- | :-- |
| no middleware, no CORS | 1.00× |
| wildcard CORS / constant `response_headers` | ~1.02× |
| allow-list CORS | ~1.15× |
| on-scope `intercept` (falling through) | ~1.4× |
| `observe` | ~1.6× |
| `response_headers` method | ~1.9× |

The tiers order the design: reach for the constant tier when a value is fixed, scope
intercepts to the verbs that need them, and treat the method tier as the last resort it
is. Every cost above is opt-in and per-covered-route — uncovered routes stay at 1.00×.
