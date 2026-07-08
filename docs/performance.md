# Performance

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account.

**The short version:** across four workloads benchmarked side by side against seven
other frameworks — Python (Litestar, FastAPI, Blacksheep, Robyn, Flask), Go (Gin), and
Bun (Elysia) — jero led the Python frameworks tested in every scenario. On the pure
framework hot path (a typed JSON `GET`) it topped this benchmark table by overall
score, ahead of both the Go and the Bun service. On the I/O-bound scenarios (an
upstream proxy, a database read) Go pulled well clear — there the bottleneck is the
HTTP-client and database-driver ecosystem, not the framework, and that's a fight Python
doesn't win today.

Read the caveats. These are favourable, constrained conditions, and a microbenchmark is
not your application.

And yes — we know benchmarks are genuinely hard to do right and to do fairly. Every
framework has a configuration that flatters it, every harness makes choices that nudge
the numbers, and reasonable people disagree about what "fair" even means. This is *one*
benchmark, run one way, on one machine. We've tried to be even-handed and we show
exactly how it was produced below so you can judge for yourself — but please treat it as
a single data point, not the last word. If you have a workload that matters to you, the
only number worth trusting is the one you measure yourself.

## How the numbers were produced

The benchmark runs each framework **in isolation, one at a time**. Only one framework
server is up at any moment, alongside its own freshly-started dependencies — a Rust
upstream service (for the proxy scenario) and a fresh Postgres (for the database
scenario). Nothing else competes for the machine. This removes cross-framework
contention and shared-state effects, so each number reflects that framework alone.

- **Load generator:** [k6](https://k6.io/), a fixed virtual-user (VU) count hammering
  the service for a fixed duration.
- **Best-of-N:** every `(framework, scenario)` pair is run *N* times and the best run
  is kept. Repeating and taking the best beats down the ~3–4% run-to-run noise floor so
  the comparison reflects each framework's ceiling, not a noisy sample.
- **Single worker, single core:** every framework runs with one worker process; Go is
  pinned to `GOMAXPROCS=1`. This is a like-for-like, single-core comparison — not a
  test of how well each scales across cores.
- **Identical scenarios** — the same request scripts, the same selection logic, and the
  same scoring table for every framework.

### Run configuration

| Setting       | Value                           |
| :------------ | :------------------------------ |
| Machine       | Apple M3 Max, 36 GB             |
| Concurrency   | 100 VUs                         |
| Duration      | 30s per run                     |
| Best-of-N     | 3 runs                          |
| Workers       | 1 (Go pinned to `GOMAXPROCS=1`) |
| Python server | Granian, single worker          |

## Results

`req/s` is throughput (higher is better); `mean` and `p99` are request latency (lower is
better). `vs all` is an aggregate score across all three — a single "overall standing"
number, normalised so jero = `1.00×` in every scenario. Every framework returned 100%
successful responses in every run, so that column is omitted. Frameworks are ordered by
`vs all` within each scenario.

### 1 — `GET /info` — the pure framework path

Route → build a typed JSON response with a typed response header → encode. No I/O. This
isolates routing and serialization, and is the closest thing to a measure of the
framework's own per-request overhead.

| Framework      | req/s     | mean       | p99        | vs all    |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| **jero**       | **44.5k** | **2.22ms** | **3.73ms** | **1.00×** |
| blacksheep     | 40.3k     | 2.45ms     | 3.36ms     | 0.97×     |
| elysia *(Bun)* | 38.7k     | 2.55ms     | 3.52ms     | 0.93×     |
| gin *(Go)*     | 38.4k     | 2.57ms     | 3.79ms     | 0.90×     |
| litestar       | 35.6k     | 2.78ms     | 3.99ms     | 0.84×     |
| fastapi        | 24.5k     | 4.06ms     | 4.81ms     | 0.62×     |
| robyn          | 20.6k     | 4.83ms     | 10.46ms    | 0.42×     |
| flask          | 17.9k     | 5.56ms     | 19.29ms    | 0.31×     |

### 2 — `POST /movies` — the authed write path (JWT)

Bearer/JWT auth → msgspec decode of the request body → handler → encode → `201`. The
realistic write path for a typed JSON API.

| Framework      | req/s     | mean       | p99        | vs all    |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| gin *(Go)*     | 28.6k     | 3.46ms     | 6.39ms     | 1.06×     |
| **jero**       | **27.4k** | **3.62ms** | **6.93ms** | **1.00×** |
| elysia *(Bun)* | 24.0k     | 4.12ms     | 8.20ms     | 0.87×     |
| blacksheep     | 16.4k     | 6.05ms     | 14.58ms    | 0.55×     |
| robyn          | 15.7k     | 6.21ms     | 18.04ms    | 0.50×     |
| litestar       | 12.0k     | 8.25ms     | 22.52ms    | 0.39×     |
| flask          | 10.5k     | 9.46ms     | 48.39ms    | 0.28×     |
| fastapi        | 5.2k      | 18.97ms    | 55.64ms    | 0.17×     |

jero lands within ~5% of a hand-written Go service here, and led the Python frameworks
tested by a wide margin.

### 3 — `GET` proxy — bound by the HTTP client

The service makes an outbound HTTP call to the Rust upstream and relays the response.
The bottleneck is the HTTP client library, not the framework — which is why the whole
Python field clusters together and Go runs away.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 15.1k    | 6.58ms      | 15.34ms     | 5.35×     |
| elysia *(Bun)* | 11.2k    | 8.77ms      | 21.11ms     | 3.96×     |
| **jero**       | **3.2k** | **31.56ms** | **102.24ms**| **1.00×** |
| litestar       | 2.8k     | 35.17ms     | 127.50ms    | 0.86×     |
| blacksheep     | 2.9k     | 33.85ms     | 158.69ms    | 0.82×     |
| fastapi        | 2.4k     | 42.21ms     | 102.92ms    | 0.82×     |
| robyn          | 2.5k     | 40.37ms     | 167.62ms    | 0.72×     |
| flask          | 2.4k     | 41.94ms     | 166.82ms    | 0.70×     |

jero led the Python frameworks tested, but Go's mature native HTTP stack is in a
different class. This gap is the ecosystem, not jero.

### 4 — `GET /users/me` — bound by the database driver

Reads a row from Postgres. The bottleneck is the database driver, so again the field
compresses and Go's native driver leads.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 16.2k    | 6.13ms      | 8.72ms      | 2.44×     |
| elysia *(Bun)* | 6.0k     | 16.45ms     | 16.77ms     | 1.02×     |
| **jero**       | **8.4k** | **11.84ms** | **33.98ms** | **1.00×** |
| blacksheep     | 7.8k     | 12.84ms     | 89.58ms     | 0.69×     |
| litestar       | 6.3k     | 15.77ms     | 141.70ms    | 0.51×     |
| robyn          | 4.6k     | 21.87ms     | 172.95ms    | 0.39×     |
| fastapi        | 3.4k     | 29.26ms     | 104.84ms    | 0.38×     |
| flask          | 1.3k     | 78.03ms     | 210.18ms    | 0.15×     |

jero led the Python frameworks tested again. Go was well ahead; Bun's lower p99 edged
jero on aggregate score despite lower throughput and higher mean latency.

## How to read this

- **jero leads the Python frameworks tested in all four scenarios.** That is the
  durable claim.
- **On the pure framework path it beats even Go and Bun.** That result is real but
  narrow: an in-memory JSON path plays directly to Python + msgspec's strengths and to
  the Rust HTTP layer underneath. It is *not* evidence that Python is faster than Go in
  general — and we are not making that claim.
- **On I/O-bound paths, Go is well ahead.** When the work is an outbound HTTP call or a
  database query, the framework is barely in the picture; the HTTP client and database
  driver decide it, and Go's native libraries dominate. jero stays ahead of the Python
  frameworks tested, which is the most it can do there.
- **A benchmark is not your app.** Single worker, single core, localhost, fixed
  payloads, best-of-N. Real workloads have more moving parts. Treat these as directional
  evidence that jero's per-request overhead is low — not as a promise about your
  production numbers.

Where jero's design earns these numbers: all type introspection happens **once, at
startup**. The request path is dict lookup → msgspec decode → handler call → encode, and
nothing is ever added to it. See [the design philosophy](index.md) for why that's a
deliberate, non-negotiable bet.

## Measure it yourself

The numbers above need a load generator, a server, and patience. This one doesn't:
a single self-contained script that drives each framework **in-process as a bare ASGI
callable** — no server, no sockets — so it isolates pure framework overhead (routing,
binding/validation, serialization) on your own machine in under a minute.

It benchmarks jero, Starlette, BlackSheep, and FastAPI against a hand-rolled raw ASGI
app serving the same three-endpoint API. The raw app uses no framework but is kept
honest: it routes by hand, extracts the path and query values, does a typed validating
msgspec decode of the POST body, and a typed msgspec encode of every response. It
skips everything a framework gives you (404/405 semantics, HEAD/OPTIONS, content-type
checks, error envelopes) — that's the point: it is the floor those conveniences sit on.

Copy, paste, run. Dependencies are declared inline (PEP 723), so
[uv](https://docs.astral.sh/uv/) resolves them on the fly; pin versions by editing the
`dependencies` block, and tweak the apps or scenarios as you see fit.

```bash
uv run - <<'EOF'
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "blacksheep",
#     "fastapi",
#     "jero",
#     "msgspec",
#     "rich",
#     "starlette",
# ]
# ///
"""In-process ASGI micro-benchmark: jero vs Starlette vs BlackSheep vs FastAPI vs a
hand-rolled raw ASGI app, all serving the same three-endpoint API.

Every app is driven directly as an ASGI callable — no server, no sockets — so what you
measure is each framework's own per-request overhead: routing, binding/validation, and
serialization. That makes the numbers a per-core ceiling, not a production promise.

Scenarios (identical raw ASGI scopes for every app):
  plain   GET  /ping                      -> tiny JSON body
  bind    GET  /items/{item_id}?limit=10  -> path + query binding, JSON body
  body    POST /items (JSON, 3 fields)    -> body decode/validation, JSON echo

Fairness notes:
  - The "raw ASGI" app uses no framework but still does honest work with msgspec:
    hand-rolled routing, path/query extraction, a typed validating decode of the POST
    body, and a typed encode of every response. It skips everything a framework gives
    you (404/405 semantics, HEAD/OPTIONS, content-type checks, error envelopes).
  - Each framework app is written the way its own documentation recommends.
  - Starlette has no built-in validation, so its POST does strictly less work.
  - BlackSheep uses its documented msgspec JSON hook (its fastest configuration).
  - Tweak freely: pin versions in the dependencies block above, change N_MEASURE,
    add a framework, add a scenario.
"""

import asyncio
import time

N_WARMUP = 2_000
N_MEASURE = 20_000
BEST_OF = 3

POST_BODY_CAMEL = b'{"name":"gizmo","priceCents":499,"tags":["a","b"]}'
POST_BODY_SNAKE = b'{"name":"gizmo","price_cents":499,"tags":["a","b"]}'


# --------------------------------------------------------------------- harness
def make_scope(method: str, path: str, query: bytes = b"", body: bool = False) -> dict:
    headers = [(b"host", b"bench")]
    if body:
        headers.append((b"content-type", b"application/json"))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }


SCENARIOS = [
    ("plain GET", make_scope("GET", "/ping"), False),
    ("path+query GET", make_scope("GET", "/items/abc123", query=b"limit=10"), False),
    ("POST w/ body", make_scope("POST", "/items", body=True), True),
]


async def run_lifespan_startup(app) -> None:
    """Send lifespan.startup and wait for completion (frameworks wire routes here)."""
    started = asyncio.Event()

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        if message["type"] == "lifespan.startup.failed":
            raise RuntimeError(message.get("message", "lifespan startup failed"))
        if message["type"] == "lifespan.startup.complete":
            started.set()

    task = asyncio.ensure_future(app({"type": "lifespan"}, receive, send))
    await started.wait()
    task.cancel()


async def bench_one(app, scope_template: dict, body: bytes | None) -> float:
    """Seconds for N_MEASURE requests (asserts 2xx on every response)."""

    def make_receive():
        sent = False

        async def receive():
            nonlocal sent
            if body is not None and not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return receive

    async def send(message):
        if message["type"] == "http.response.start":
            assert 200 <= message["status"] < 300, f"unexpected status {message['status']}"

    async def one():
        await app(dict(scope_template), make_receive(), send)

    for _ in range(N_WARMUP):
        await one()
    start = time.perf_counter()
    for _ in range(N_MEASURE):
        await one()
    return time.perf_counter() - start


# ------------------------------------------------------- raw ASGI (honest work)
def build_raw():
    import msgspec

    class Ok(msgspec.Struct):
        ok: bool

    class ItemIn(msgspec.Struct, rename="camel"):
        name: str
        price_cents: int
        tags: list[str]

    class Item(msgspec.Struct, rename="camel"):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    encoder = msgspec.json.Encoder()
    item_in_decoder = msgspec.json.Decoder(ItemIn)

    async def send_json(send, payload: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"%d" % len(payload)),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def app(scope, receive, send):
        method, path = scope["method"], scope["path"]
        if method == "GET" and path == "/ping":
            await send_json(send, encoder.encode(Ok(ok=True)))
        elif method == "GET" and path.startswith("/items/"):
            item_id = path[len("/items/") :]
            limit = 20
            for pair in scope["query_string"].decode("latin-1").split("&"):
                key, _, value = pair.partition("=")
                if key == "limit" and value:
                    limit = int(value)
            item = Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])
            await send_json(send, encoder.encode(item))
        elif method == "POST" and path == "/items":
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            data = item_in_decoder.decode(b"".join(chunks))
            item = Item(id="new", name=data.name, price_cents=data.price_cents, tags=data.tags)
            await send_json(send, encoder.encode(item))
        else:
            raise AssertionError(f"unrouted request {method} {path}")

    return app


# ------------------------------------------------------------------------ jero
def build_jero():
    from jero import BaseApp, Endpoint, Struct

    class Ok(Struct):
        ok: bool

    class ItemPath(Struct):
        item_id: str

    class PageParams(Struct):
        limit: int = 20

    class ItemIn(Struct, rename="camel"):
        name: str
        price_cents: int
        tags: list[str]

    class Item(Struct, rename="camel"):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    class PingEndpoint(Endpoint, path="/ping"):
        async def get(self) -> Ok:
            return Ok(ok=True)

    class ItemEndpoint(Endpoint, path="/items/{item_id}"):
        async def get(self, path: ItemPath, params: PageParams) -> Item:
            return Item(id=path.item_id, name="gizmo", price_cents=params.limit, tags=["a"])

    class ItemsEndpoint(Endpoint, path="/items"):
        async def post(self, json: ItemIn) -> Item:
            return Item(id="new", name=json.name, price_cents=json.price_cents, tags=json.tags)

    class App(BaseApp):
        async def wire(self) -> None:
            self.include_endpoint(PingEndpoint())
            self.include_endpoint(ItemEndpoint())
            self.include_endpoint(ItemsEndpoint())

    return App()


# ------------------------------------------------------------------- starlette
def build_starlette():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ping(request):
        return JSONResponse({"ok": True})

    async def get_item(request):
        item_id = request.path_params["item_id"]
        limit = int(request.query_params.get("limit", "20"))
        return JSONResponse({"id": item_id, "name": "gizmo", "priceCents": limit, "tags": ["a"]})

    async def create_item(request):
        data = await request.json()  # no validation: Starlette has none built in
        return JSONResponse(
            {
                "id": "new",
                "name": data["name"],
                "priceCents": data["priceCents"],
                "tags": data["tags"],
            }
        )

    return Starlette(
        routes=[
            Route("/ping", ping),
            Route("/items/{item_id}", get_item),
            Route("/items", create_item, methods=["POST"]),
        ]
    )


# ------------------------------------------------------------------ blacksheep
def build_blacksheep():
    from dataclasses import dataclass

    import msgspec
    from blacksheep import Application, FromJSON, get, post
    from blacksheep.settings.json import json_settings

    # BlackSheep's documented hook for a faster JSON codec.
    def msgspec_dumps(obj) -> str:
        return msgspec.json.encode(obj).decode("utf-8")

    json_settings.use(loads=msgspec.json.decode, dumps=msgspec_dumps)

    app = Application()

    @dataclass
    class ItemIn:
        name: str
        price_cents: int
        tags: list[str]

    @dataclass
    class Item:
        id: str
        name: str
        price_cents: int
        tags: list[str]

    @get("/ping")
    async def ping():
        return {"ok": True}

    @get("/items/{item_id}")
    async def get_item(item_id: str, limit: int = 20):
        return Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])

    @post("/items")
    async def create_item(item: FromJSON[ItemIn]):
        data = item.value
        return Item(id="new", name=data.name, price_cents=data.price_cents, tags=data.tags)

    return app


# --------------------------------------------------------------------- fastapi
def build_fastapi():
    from fastapi import FastAPI
    from pydantic import BaseModel

    class ItemIn(BaseModel):
        name: str
        price_cents: int
        tags: list[str]

    class Item(BaseModel):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/items/{item_id}")
    async def get_item(item_id: str, limit: int = 20) -> Item:
        return Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])

    @app.post("/items")
    async def create_item(item: ItemIn) -> Item:
        return Item(id="new", name=item.name, price_cents=item.price_cents, tags=item.tags)

    return app


# ------------------------------------------------------------------------ main
async def main() -> None:
    # (label, builder, needs_lifespan, post_body)
    candidates = [
        ("raw (msgspec)", build_raw, False, POST_BODY_CAMEL),
        ("jero", build_jero, True, POST_BODY_CAMEL),
        ("Starlette", build_starlette, True, POST_BODY_CAMEL),
        ("BlackSheep", build_blacksheep, True, POST_BODY_SNAKE),
        ("FastAPI", build_fastapi, True, POST_BODY_SNAKE),
    ]
    results: dict[str, dict[str, float]] = {}
    for name, builder, needs_lifespan, post_body in candidates:
        try:
            app = builder()
        except ImportError:
            print(f"{name}: skipped (not installed)")
            continue
        if needs_lifespan:
            await run_lifespan_startup(app)
        results[name] = {}
        for scenario, scope, has_body in SCENARIOS:
            body = post_body if has_body else None
            results[name][scenario] = min(
                [await bench_one(app, scope, body) for _ in range(BEST_OF)]
            )

    from rich import box
    from rich.console import Console
    from rich.table import Table

    def compact(rps: float) -> str:
        return f"{rps / 1e6:.2f}M" if rps >= 1e6 else f"{rps / 1e3:.0f}k"

    # Multipliers are per-request cost relative to jero (or, if jero was removed from
    # the run, relative to the first app benchmarked).
    base = results.get("jero") or next(iter(results.values()))
    table = Table(
        title="Framework overhead per request",
        caption=(
            f"req/s · µs per request · cost relative to jero (lower is cheaper)\n"
            f"{N_MEASURE:,} requests per cell, best of {BEST_OF}, "
            f"in-process — no server, no sockets"
        ),
        box=box.ROUNDED,
        title_style="bold",
        caption_style="dim",
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("framework", style="cyan", no_wrap=True)
    for scenario, _, _ in SCENARIOS:
        table.add_column(scenario, justify="right", no_wrap=True)
    for name, cells in results.items():
        row = ["[bold white]jero[/]" if name == "jero" else name]
        for scenario, _, _ in SCENARIOS:
            elapsed = cells[scenario]
            us = elapsed / N_MEASURE * 1e6
            mult = elapsed / base[scenario]
            figures = f"{compact(N_MEASURE / elapsed):>5}  {us:>5.1f}µs  {mult:>5.1f}×"
            row.append(f"[bold]{figures}[/]" if name == "jero" else figures)
        table.add_row(*row)
    # A fixed width keeps the table identical in any terminal (or piped to a file).
    Console(width=96).print(table)


asyncio.run(main())
EOF
```
