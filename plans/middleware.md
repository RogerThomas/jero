# Plan: Compiled middleware (and CORS as its first consumer)

Status: **designed, benchmarked, not built.** The mechanism was validated with
in-process spikes (numbers below); the protocol shape and scoping decisions are
locked. Build in the staged order at the bottom.

Note: member spellings here (`add_middleware`, `include_cors`) predate the
`public-surface.md` naming rule — final spellings follow that rule at build time.

## Goal

Give users a way to hang cross-cutting behavior on the request path without
giving up jero's core invariant: **features you don't use don't exist at request
time, and features you do use cost exactly what they must.**

The classic ASGI middleware shape (an onion of app wrappers, each an async
callable wrapping `send`) is rejected outright. It costs a coroutine plus
allocations per layer per request, on every request, whether or not the layer
applies. Measured on jero's hot path, one optimized onion CORS layer costs
**1.49×** — half of jero's entire per-request budget.

Instead, middleware is a **typed, hand-wired object whose hooks are introspected
and compiled at wiring** (the `ExceptionHandler` pattern). Each hook kind has a
fixed, known cost, and wiring compiles only what a middleware actually defines:

| hook | when it runs | per-request cost |
| :-- | :-- | :-- |
| `response_headers` (constant attribute) | never — baked into route header blocks at `_finalize` | zero |
| `intercept` + `intercept_methods` | only on the declared verbs, pre-dispatch | zero off-scope; one table hit + header scan on-scope |
| `response_headers(...)` (method) | every covered response, as it leaves | one scan + call + merge (~0.3–0.5µs) |
| `observe(...)` | after the response starts, fire-and-forget | one call |

### Spike evidence (in-process ASGI harness, plain GET vs jero baseline)

- onion CORS wrapper: **1.49×** — the rejected mechanism
- sync-shim wrapper (no extra coroutine): 1.35× — better, still a wrapper
- CORS compiled into the route table: **0.99–1.04×** — statistically free
- user-built wildcard CORS on this protocol: 1.11× measured, ~**1.04×** once the
  branch lives inside `BaseApp.__call__` instead of a prototype subclass
- user-built allow-list CORS (dynamic header echo): 1.50× measured, ~**1.3×**
  with pairs threaded into the sender instead of a send wrapper
- everything disabled: **1.00×** — within run-to-run noise of today's core

Scripts: `cors_bench.py` / `middleware_bench.py` and `cors_prototype.patch`
(the 96-line `_headers_tail` core spike) from the design session.

## Public API

### New exports

```python
type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
```

DECIDED: promote the existing private `_HttpMethod` (core) — the name is
`method` everywhere public (ASGI scope, `OperationSpec.method`, `allow_methods`);
"verb" stays prose-only.

```python
class Request[H: Struct](Struct, frozen=True):
    method: HttpMethod
    path: str
    headers: H            # the middleware's own Struct; fields decide what gets bound
    received_at: float    # perf_counter() at dispatch; stamped only when a hook needs it
```

The read-only typed request view middleware hooks receive. `headers` binds
exactly like auth's `headers` parameter: wiring introspects the hook's
annotation, and the compiled dispatcher scans the raw pairs for just the keys
the Struct's fields name.

### The middleware protocol (structural, no base class)

A middleware object may define any of the following; `add_middleware` /
`middleware=` introspects and validates the signatures fail-loud at wiring
(`WiringError`), exactly like `_include_exception_handler` does:

```python
class ExampleMiddleware:
    # static scope declaration: intercept is compiled into these verbs only
    intercept_methods: ClassVar[tuple[HttpMethod, ...]] = ("OPTIONS",)

    # EITHER a constant (baked into route header blocks at _finalize — free) ...
    response_headers: ClassVar[SecurityHeaders] = SecurityHeaders(...)

    # ... OR a per-request method (the only hook that costs on the hot path)
    def response_headers(self, request: Request[OriginHeaders]) -> CORSHeaders | None: ...

    # answer instead of routing; None falls through. Sync or async (auth-style
    # iscoroutinefunction compile). Returns the same response types handlers use.
    def intercept(self, request: Request[PreflightHeaders]) -> BytesResponse[PH] | None: ...

    # observability: sees the outcome, cannot touch it. Exceptions logged+swallowed.
    def observe(self, request: Request[H], status: int, duration: float) -> None: ...
```

DECIDED — capability boundary: middleware can **answer** a request
(`intercept`), **add response headers** (`response_headers`), and **watch**
(`observe`). It can never rewrite a handler's body or status: that requires
buffering or wrapping every response (the onion price), belongs in granian or
the reverse proxy (compression, caching), and jero refuses it on principle
rather than covering it slowly.

### Registration: global and per-include

```python
class App(BaseApp):
    async def wire(self) -> None:
        self.add_middleware(TimingMiddleware())                     # every route
        self._include_resource(WidgetResource(), auth=auth)          # covered by globals
        self._include_endpoint(
            AdminEndpoint(),
            middleware=(AdminGateMiddleware(),),                    # + scoped
        )
```

DECIDED: `middleware=` is an include-time keyword beside `auth=` — middleware
scope is deployment policy, not resource identity, so it does not live on the
`Endpoint`/`Resource` class. Global intercepts run pre-routing (they can answer
preflights for paths that would 404); include-scoped ones run post-resolve,
pre-bind. Order is registration order, globals first; the first intercept
returning a response wins.

### CORS (built-in, first consumer of the same machinery)

```python
class CORS(Struct, frozen=True):
    allow_origins: tuple[str, ...] | Literal["*"] = "*"
    allow_methods: tuple[HttpMethod, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
    allow_headers: tuple[str, ...] = ("content-type", "authorization")
    allow_credentials: bool = False
    max_age: int = 600

    OFF: ClassVar["CORS"]   # sentinel: opt a route out of an app-wide default
```

```python
self.include_cors(CORS())                                   # app-wide default
self._include_resource(WidgetResource(), cors=PUBLIC)        # override per include
self._include_endpoint(MetricsEndpoint(), cors=CORS.OFF)     # opt out
```

DECIDED:

- Wiring validation, fail-loud: `allow_credentials=True` with `"*"` is a
  `WiringError` (spec-forbidden); malformed origins and unknown methods too.
- `"*"` compiles to constant pairs in every covered route's header block (the
  free tier). An origin allow-list compiles to one frozenset lookup + origin
  echo + a precomputed `Vary: Origin` pair in the sender — no wrapper.
- Preflights ride the existing cold OPTIONS branch. The answer is per
  `(path, requested method)` — `Access-Control-Request-Method` selects which
  route's config replies, so `GET` public / `POST` restricted on one path works.
- Error responses carry the route's CORS pairs (a browser page must be able to
  *read* the 401/422 problem body). Unrouted 404s carry the app default only.
- Precedence: include-level `cors=` overrides the `include_cors` default;
  omitted inherits; `CORS.OFF` removes; no default + no `cors=` means no CORS
  (pure opt-in works by skipping `include_cors`).

## Request-path semantics

```
request arrives
  1. global intercepts (verb-scoped, registration order) — first response wins
  2. routing (static hit / dynamic match; 404/405/OPTIONS fallthrough)
  3. include-scoped intercepts, same rule
  4. auth → bind → handler
  5. response_headers hooks merge onto WHATEVER response leaves
     (handler success, exception-handler response, problem body, short-circuit)
  6. observe hooks fire after the response starts
```

- Intercept runs **before auth** (a preflight carries no credentials). A hook
  that must run post-auth is a future hook kind, not a reinterpretation of this
  one.
- Intercept scoping sees the **wire method**: `HEAD` is its own entry, even
  though routing serves it from `GET` handlers.
- An exception inside `intercept`/`response_headers` enters the same funnel as
  handler exceptions (custom handlers get their shot, then the generic 500
  problem); `observe` exceptions are logged and swallowed.
- Middleware pairs for `content-length`/`content-type` are rejected at wiring —
  the senders own those.
- Exception handlers and middleware cannot collide: they trigger on different
  things (a raise vs request shape), and at most one system produces the body
  for a given request.

## What this does not do (and the documented idiom instead)

- **Body/status transformation** (compression, ETag/304, rewriting): refused;
  point at granian/reverse-proxy in the docs.
- **Receive-side wrapping** (streaming upload limits beyond the
  `content-length` header check, which `intercept` covers): out of scope.
- **Middleware→handler state** (`request.state.tenant`): not offered. The idiom
  is jero's existing typed sources — bind the header in the handler, or carry it
  on the auth `user`.

With `observe` and async `intercept` included, coverage of real-world middleware
usage is ~90%+; the remainder is the body-transformation family above.

## OPEN

1. `observe` timing source: is `duration = perf_counter() - received_at` at
   response-start good enough, or should it run at response-body-complete
   (streaming makes "complete" fuzzy)? Leaning response-start.
2. Should `response_headers` (method form) also receive the response `status`?
   Useful (e.g. only decorate errors), but couples the hook to the sender.
   Leaning no for v1.
3. Exact rejection rules for duplicate header keys between two middlewares'
   constant pairs (wiring can check constants; dynamic collisions cannot be
   checked and append per HTTP semantics).

## Staged build order

1. **Sender seam**: thread a per-route constant `headers_tail` through *all*
   senders (inline JSON, JSONResponse/Bytes/streaming, and the error/problem
   sender). This is the `cors_prototype.patch` spike done properly. Zero
   behavior change; benchmark guard: disabled == baseline.
2. **`HttpMethod` + `Request`** public types; `include_cors` + `CORS` +
   per-include `cors=`/`CORS.OFF` compiled onto the seam from (1). Docs page
   for CORS; demo_app enables it; tests incl. preflight-per-method and
   errors-carry-pairs.
3. **`add_middleware` / `middleware=`**: protocol introspection + validation
   (CompiledMiddleware mirroring CompiledExceptionHandler), constant
   `response_headers` tier, sync `intercept` with verb tables and compiled
   header scanners.
4. **Dynamic tiers**: `response_headers` method form threaded into senders,
   async `intercept`, `observe`, `received_at` stamping (only when a dynamic
   hook covers the route).
5. **Docs** (`docs/guide/middleware.md`: the tier cost table is the centerpiece;
   every example a complete runnable app), rebuild-CORS-yourself example,
   `AGENTS.md` public-surface update, benchmark row in `docs/performance.md`.
