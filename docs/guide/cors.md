# CORS

Browsers refuse to let a page on one origin read responses from another unless the
server opts in with CORS headers. jero ships CORS as a built-in policy object — not a
wrapper around your app — compiled at wiring into exactly the header pairs each route
needs.

The policy is a `CORS` Struct:

```python
from jero import CORS

CORS(
    allow_origins=("https://app.example",),  # or "*" (the default)
    allow_methods=("GET", "POST", "PUT", "PATCH", "DELETE"),
    allow_headers=("content-type", "authorization"),
    allow_credentials=False,
    max_age=600,
)
```

Everything is validated loud at startup: `allow_credentials=True` with `"*"` is
spec-forbidden and a `WiringError`, as are malformed origins (`https://app.example/` is
a URL, not an origin) and unknown methods.

## What it costs

Which tier a policy compiles to depends only on `allow_origins`:

| policy | per-request cost |
| :-- | :-- |
| `allow_origins="*"` | zero — constant pairs baked into each covered route's header block at wiring |
| an origin tuple | one frozenset lookup + origin echo per response, plus a constant `Vary: Origin` pair |

Preflights (`OPTIONS` with `Access-Control-Request-Method`) ride the existing cold
OPTIONS branch and never touch the hot path.

## A complete example

```python
from jero import CORS, BaseApp, Endpoint, Struct


class Status(Struct):
    ok: bool


class HealthEndpoint(Endpoint, path="/healthz"):
    def get(self) -> Status:
        return Status(ok=True)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_cors(CORS())            # app-wide default: any origin
        self._include_endpoint(HealthEndpoint())


app = App()
```

Run it with `granian --interface asgi app:app` and every response from `/healthz`
carries `access-control-allow-origin: *`.

## Scoping: default, override, opt out

`_include_cors` sets the app-wide default; every include inherits it. The `cors=`
keyword on `_include_resource` / `_include_endpoint` overrides it per include, and
`CORS.OFF` removes it — CORS scope is deployment policy, so it lives at the mount, not
on the `Resource`/`Endpoint` class:

```python
from jero import CORS, BaseApp, Endpoint, Struct


class Value(Struct):
    value: int


class PublicEndpoint(Endpoint, path="/public"):
    def get(self) -> Value:
        return Value(value=1)


class PartnerEndpoint(Endpoint, path="/partner"):
    def get(self) -> Value:
        return Value(value=2)


class InternalEndpoint(Endpoint, path="/internal"):
    def get(self) -> Value:
        return Value(value=3)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_cors(CORS())  # the default: any origin
        self._include_endpoint(PublicEndpoint())  # inherits the default
        self._include_endpoint(
            PartnerEndpoint(),
            cors=CORS(allow_origins=("https://partner.example",), allow_credentials=True),
        )
        self._include_endpoint(InternalEndpoint(), cors=CORS.OFF)  # no CORS at all


app = App()
```

An app that never calls `_include_cors` serves no CORS headers anywhere — the feature
is pure opt-in, and includes can still opt in individually with their own `cors=`.

## Semantics worth knowing

- **Preflights answer per (path, requested method).** The
  `Access-Control-Request-Method` header selects *which route's* policy replies, so a
  public `GET` and a restricted `POST` on one path each answer preflights with their
  own policy.
- **Error responses carry the route's pairs.** A browser page must be able to *read*
  the 401/422 problem body, so problem responses leave with the failing route's CORS
  pairs on them. Unrouted 404s carry the app default only.
- **Allow-list responses always carry `Vary: Origin`** — even when the origin didn't
  match — so shared caches never serve one origin's response to another.
- **`HEAD` is its own method** in `allow_methods`, even though routing serves it from
  `GET` handlers.

CORS is the first consumer of the compiled middleware machinery — the
[middleware guide](middleware.md) shows the same tiers as a user-facing protocol,
including how to rebuild CORS yourself.
